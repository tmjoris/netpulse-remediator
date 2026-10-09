"""Active-probe telemetry collector.

For each configured link, ping the far end out of that specific interface
(so the probe measures the link even when routing has moved traffic off it)
and read the interface byte counters around the probe to estimate
utilization. One sample per link per cycle.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .config import ProbeSpec
from .devices import Transport
from .models import Sample, utcnow
from .topology import Topology

_SAFE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_COUNTS = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"= [\d.]+/([\d.]+)/")


@dataclass(frozen=True)
class PingResult:
    loss_pct: float
    avg_rtt_ms: float | None


def parse_ping(output: str) -> PingResult:
    """Parse BusyBox or iputils ``ping -q`` summary output."""
    counts = _COUNTS.search(output)
    if counts is None:
        raise ValueError("no ping summary in output")
    sent, received = int(counts.group(1)), int(counts.group(2))
    rtt = _RTT.search(output)
    loss = 100.0 * (sent - received) / sent if sent else 100.0
    return PingResult(loss, float(rtt.group(1)) if rtt else None)


class ProbeCollector:
    def __init__(
        self,
        transport: Transport,
        probes: Iterable[ProbeSpec],
        topology: Topology,
        *,
        count: int = 20,
        interval_s: float = 0.1,
        timeout_s: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.probes = tuple(probes)
        self.topology = topology
        self.count = count
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._clock = clock
        for spec in self.probes:
            for value in (spec.ref.interface, spec.target):
                if not _SAFE.match(value):
                    raise ValueError(f"probe {spec.ref}: unsafe characters in {value!r}")

    def script(self, spec: ProbeSpec) -> str:
        stats = f"/sys/class/net/{spec.ref.interface}/statistics"
        counters = f"cat {stats}/tx_bytes {stats}/rx_bytes"
        ping = (
            f"ping -q -c {self.count} -i {self.interval_s:g} -W {self.timeout_s} "
            f"-I {spec.ref.interface} {spec.target}"
        )
        # ping exits non-zero on total loss; that is a measurement, not an error.
        return f"{counters}; {ping} || true; {counters}"

    def measure(self, spec: ProbeSpec) -> Sample:
        started = self._clock()
        output = self.transport.run(spec.ref.device, ["sh", "-c", self.script(spec)])
        elapsed = max(self._clock() - started, 1e-3)
        lines = output.strip().splitlines()
        tx0, rx0, tx1, rx1 = (int(v) for v in (lines[0], lines[1], lines[-2], lines[-1]))
        ping = parse_ping(output)

        group = self.topology.group_for(spec.ref)
        member = group.member(spec.ref) if group else None
        utilization = 0.0
        if member is not None:
            bits = max(tx1 - tx0, rx1 - rx0) * 8
            utilization = min(100.0, bits / elapsed / (member.capacity_gbps * 1e9) * 100)
        # No replies at all: report the probe timeout as latency (worst case seen).
        latency = ping.avg_rtt_ms if ping.avg_rtt_ms is not None else self.timeout_s * 1000.0
        return Sample(
            spec.ref.device,
            spec.ref.interface,
            utcnow(),
            round(latency, 3),
            round(ping.loss_pct, 2),
            round(utilization, 3),
        )

    def collect(self) -> list[Sample]:
        if not self.probes:
            return []
        with ThreadPoolExecutor(max_workers=min(16, len(self.probes))) as pool:
            return list(pool.map(self.measure, self.probes))

import pytest

from netpulse.collector import ProbeCollector, parse_ping
from netpulse.config import ProbeSpec
from netpulse.models import InterfaceRef
from netpulse.topology import LinkGroup, Member, Topology

BUSYBOX = """PING 10.0.1.3 (10.0.1.3): 56 data bytes

--- 10.0.1.3 ping statistics ---
20 packets transmitted, 15 packets received, 25% packet loss
round-trip min/avg/max = 0.063/0.250/0.093 ms
"""
IPUTILS = """--- 10.0.1.3 ping statistics ---
10 packets transmitted, 10 received, 0% packet loss, time 904ms
rtt min/avg/max/mdev = 0.040/1.500/0.060/0.010 ms
"""
TOTAL_LOSS = "--- 10.0.1.3 ping statistics ---\n5 packets transmitted, 0 packets received, 100% packet loss\n"

REF = InterfaceRef("r1", "lnk1")
TOPOLOGY = Topology([LinkGroup("g", (Member(REF, 1.0), Member(InterfaceRef("r1", "lnk2"), 1.0)))])


def test_parse_ping_formats() -> None:
    assert parse_ping(BUSYBOX).loss_pct == 25.0 and parse_ping(BUSYBOX).avg_rtt_ms == 0.25
    assert parse_ping(IPUTILS).loss_pct == 0.0 and parse_ping(IPUTILS).avg_rtt_ms == 1.5
    assert parse_ping(TOTAL_LOSS).loss_pct == 100.0 and parse_ping(TOTAL_LOSS).avg_rtt_ms is None
    with pytest.raises(ValueError):
        parse_ping("ping: bad address")


class CannedTransport:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, device: str, argv: list[str]) -> str:
        self.calls.append((device, argv))
        return self.output


def test_measure_builds_a_sample_with_utilization() -> None:
    # 250 MB transmitted during a 2 s probe on a 1 Gbps link = 100%; 25 MB = 10%.
    output = f"1000\n500\n{BUSYBOX}26001000\n600\n"
    ticks = iter([0.0, 2.0])
    transport = CannedTransport(output)
    collector = ProbeCollector(transport, [ProbeSpec(REF, "10.0.1.3")], TOPOLOGY, clock=lambda: next(ticks))
    (sample,) = collector.collect()
    assert sample.packet_loss_pct == 25.0
    assert sample.latency_ms == 0.25
    assert sample.utilization_pct == pytest.approx(10.4)
    script = transport.calls[0][1][-1]
    assert "-I lnk1 10.0.1.3" in script and "|| true" in script


def test_total_loss_reports_timeout_latency() -> None:
    collector = ProbeCollector(
        CannedTransport(f"0\n0\n{TOTAL_LOSS}0\n0\n"), [ProbeSpec(REF, "10.0.1.3")], TOPOLOGY
    )
    (sample,) = collector.collect()
    assert sample.packet_loss_pct == 100.0 and sample.latency_ms == 1000.0


def test_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ProbeCollector(CannedTransport(""), [ProbeSpec(REF, "10.0.1.3; reboot")], TOPOLOGY)
    assert ProbeCollector(CannedTransport(""), [], TOPOLOGY).collect() == []

"""Deterministic failure-scenario lab.

Each scenario generates per-member telemetry for a small two-group topology,
including traffic redistribution: when a member is drained its share of the
group's demand moves to the survivors, so drains have visible consequences.
Scenarios run in-process against a ``LabExecutor`` or over HTTP against a
running NetPulse server.
"""

from __future__ import annotations

import json
import random
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Settings
from .engine import RemediationEngine
from .executors import LabExecutor
from .models import Action, InterfaceRef, Sample, to_jsonable
from .topology import LinkGroup, Member, Topology


def _bundle(name: str, device: str, interfaces: list[str], capacity: float = 100) -> LinkGroup:
    return LinkGroup(name, tuple(Member(InterfaceRef(device, i), capacity) for i in interfaces))


LAB_TOPOLOGY = Topology(
    [
        _bundle("dub-lon-bundle", "edge-dub-01", ["et-0/0/0", "et-0/0/1", "et-0/0/2", "et-0/0/3"]),
        _bundle("dub-ams-ecmp", "edge-dub-02", ["et-0/0/0", "et-0/0/1"]),
    ]
)
LON = [InterfaceRef("edge-dub-01", f"et-0/0/{i}") for i in range(4)]
AMS = [InterfaceRef("edge-dub-02", f"et-0/0/{i}") for i in range(2)]


@dataclass(frozen=True)
class Fault:
    target: InterfaceRef
    start_min: float
    end_min: float
    loss_pct: float
    extra_latency_ms: float = 30.0


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    expect: str
    duration_min: int
    faults: tuple[Fault, ...]
    demand_gbps: dict[str, float] = field(
        default_factory=lambda: {"dub-lon-bundle": 150.0, "dub-ams-ecmp": 120.0}
    )
    # (action, target, "fail" | "ignore") applied to the LabExecutor
    executor_faults: tuple[tuple[Action, InterfaceRef, str], ...] = ()


def _flaps(target: InterfaceRef, starts: list[float], length: float) -> tuple[Fault, ...]:
    return tuple(Fault(target, s, s + length, 18.0) for s in starts)


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            "gray-failure",
            "One bundle member silently drops ~15% of packets (a dirty optic); routing still sees it up.",
            "drain it (survivors go 38% -> 50%), resolve, soak 15 min, then undrain automatically",
            45,
            (Fault(LON[1], 3, 15, 15.0),),
        ),
        Scenario(
            "insufficient-capacity",
            "A member of a two-link ECMP group at 60% utilization starts losing 12% of packets.",
            "refuse to drain (the survivor would run at 120%) and escalate to a human",
            40,
            (Fault(AMS[0], 3, 30, 12.0),),
        ),
        Scenario(
            "correlated-failure",
            "A line-card fault hits three of four bundle members at once.",
            "drain two (fleet limit and capacity allow it), block the third, then undrain after recovery",
            55,
            tuple(Fault(ref, 3, 25, 20.0) for ref in LON[:3]),
        ),
        Scenario(
            "flapping",
            "A member degrades for 5 minutes every 35 minutes.",
            "drain and undrain twice, then hold the third drain for a human (flap damping)",
            100,
            _flaps(LON[3], [3, 38, 73], 5),
        ),
        Scenario(
            "change-verification",
            "The device accepts the first drain but it never takes effect.",
            "post-check fails and the change is rolled back; after the cooldown the retry succeeds",
            45,
            (Fault(LON[2], 3, 25, 15.0),),
            executor_faults=((Action.DRAIN, LON[2], "ignore"),),
        ),
    ]
}


def lab_settings(audit_path: Path, policy: Settings | None = None) -> Settings:
    """Lab topology and executor, with detection/safety policy from ``policy`` if given."""
    base = policy or Settings()
    return replace(base, topology=LAB_TOPOLOGY, executor="lab", audit_path=audit_path, api_token=None)


class TelemetryModel:
    """Turns demand, drains and faults into per-member samples."""

    def __init__(self, scenario: Scenario, topology: Topology, seed: int) -> None:
        self.scenario = scenario
        self.topology = topology
        self.random = random.Random(seed)

    def tick(self, at: datetime, minute: float, drained: set[InterfaceRef]) -> list[Sample]:
        samples = []
        for group in self.topology.groups:
            demand = self.scenario.demand_gbps.get(group.name, 0.0)
            active = [m for m in group.members if m.ref not in drained]
            capacity = sum(m.capacity_gbps for m in active) or 1.0
            for member in group.members:
                share = demand * member.capacity_gbps / capacity if member in active else 0.0
                samples.append(self._sample(member, share, at, minute))
        return samples

    def _sample(self, member: Member, load_gbps: float, at: datetime, minute: float) -> Sample:
        jitter = self.random.uniform
        utilization = (
            min(100.0, load_gbps / member.capacity_gbps * 100 + jitter(-1.5, 1.5)) if load_gbps else 0.0
        )
        latency = 18.0 + jitter(-2, 2)
        loss = max(0.0, jitter(-0.05, 0.1))
        if utilization > 90:  # queueing as the link approaches saturation
            latency += (utilization - 90) * 8
            loss += max(0.0, utilization - 97) * 1.5
        for fault in self.scenario.faults:
            if fault.target == member.ref and fault.start_min <= minute < fault.end_min:
                loss = fault.loss_pct + jitter(-1.5, 1.5)
                latency += fault.extra_latency_ms
        return Sample(
            member.ref.device,
            member.ref.interface,
            at,
            round(latency, 2),
            round(min(100.0, max(0.0, loss)), 3),
            round(max(0.0, utilization), 2),
        )


class Sink:
    def send(self, samples: list[Sample]) -> list[dict[str, Any]]:
        """Deliver samples, returning engine events and change records as dicts."""
        raise NotImplementedError

    def drained(self) -> set[InterfaceRef]:
        raise NotImplementedError


class InProcessSink(Sink):
    def __init__(self, engine: RemediationEngine) -> None:
        self.engine = engine

    def send(self, samples: list[Sample]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sample in samples:
            result = self.engine.ingest(sample)
            out += [{"type": "change", **to_jsonable(c)} for c in result.changes]
            out += [{"type": "event", **to_jsonable(e)} for e in result.events]
        return out

    def drained(self) -> set[InterfaceRef]:
        return set(self.engine.state.drained)


class HttpSink(Sink):
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def send(self, samples: list[Sample]) -> list[dict[str, Any]]:
        reply = self._call("POST", "/v1/telemetry", {"samples": [to_jsonable(s) for s in samples]})
        return [{"type": "change", **c} for c in reply["changes"]] + [
            {"type": "event", **e} for e in reply["events"]
        ]

    def drained(self) -> set[InterfaceRef]:
        return {InterfaceRef.parse(row["target"]) for row in self._call("GET", "/v1/drains")}

    def latest_timestamp(self) -> datetime | None:
        stamps = [
            datetime.fromisoformat(row["last_sample"]["timestamp"])
            for row in self._call("GET", "/v1/interfaces")
            if row["last_sample"]
        ]
        return max(stamps, default=None)


@dataclass
class SimulationReport:
    scenario: Scenario
    start: datetime
    records: list[dict[str, Any]]
    drained_at_end: set[InterfaceRef]

    def count(self, kind: str) -> int:
        return sum(r.get("kind") == kind for r in self.records if r["type"] == "event")

    def changes(self, action: str, *statuses: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.records
            if r["type"] == "change" and r["action"] == action and (not statuses or r["status"] in statuses)
        ]


def run_scenario(
    scenario: Scenario,
    sink: Sink,
    *,
    topology: Topology = LAB_TOPOLOGY,
    seed: int = 7,
    start: datetime | None = None,
    interval: timedelta = timedelta(seconds=30),
    on_tick: Callable[[list[dict[str, Any]]], None] | None = None,
) -> SimulationReport:
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    model = TelemetryModel(scenario, topology, seed)
    records: list[dict[str, Any]] = []
    for step in range(int(scenario.duration_min * 60 / interval.total_seconds())):
        at = start + step * interval
        minute = step * interval.total_seconds() / 60
        produced = sink.send(model.tick(at, minute, sink.drained()))
        records += produced
        if on_tick and produced:
            on_tick(produced)
    return SimulationReport(scenario, start, records, sink.drained())


def lab_engine(
    scenario: Scenario, audit_path: Path | None = None, policy: Settings | None = None
) -> RemediationEngine:
    if audit_path is None:
        audit_path = Path(tempfile.mkdtemp(prefix="netpulse-sim-")) / "audit.jsonl"
    executor = LabExecutor()
    for action, ref, mode in scenario.executor_faults:
        (executor.fail_on if mode == "fail" else executor.ignore_on).add((action, ref))
    settings = lab_settings(audit_path, policy)
    return RemediationEngine(settings, executor=executor, audit=AuditLog(audit_path, fsync=False))


def format_record(record: dict[str, Any], start: datetime) -> Iterator[str]:
    at_key = "at" if record["type"] == "event" else "requested_at"
    offset = datetime.fromisoformat(record[at_key]) - start
    stamp = f"T+{int(offset.total_seconds() // 60):3d}m{int(offset.total_seconds() % 60):02d}s"
    if record["type"] == "event":
        ident = record["incident_id"] or record["change_id"] or "-"
        yield f"{stamp}  {record['kind']:<10} {ident:<10} {record['target']:<22} {record['detail']}"
    else:
        yield (
            f"{stamp}  {'change':<10} {record['id']:<10} {record['target']:<22} "
            f"{record['action']} -> {record['status']}"
        )
        for check in record["checks"]:
            if not check["passed"]:
                yield f"{'':>10}  {'':<10} {'':<10} {'':<22}   x {check['name']}: {check['detail']}"

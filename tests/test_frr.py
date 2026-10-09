import pytest

from netpulse.audit import AuditLog
from netpulse.config import FrrOptions, Settings
from netpulse.engine import RemediationEngine
from netpulse.executors import ExecutionError
from netpulse.frr import FrrExecutor
from netpulse.models import Action, ChangeStatus, InterfaceRef
from netpulse.topology import LinkGroup, Member, Topology

from .conftest import at, sample
from .fake_frr import FakeFrr

L1, L2 = InterfaceRef("r1", "lnk1"), InterfaceRef("r1", "lnk2")
P1, P2 = InterfaceRef("r2", "lnk1"), InterfaceRef("r2", "lnk2")
OPTIONS = FrrOptions(peers={L1: P1, L2: P2}, converge_timeout_s=2)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def frr() -> FakeFrr:
    return FakeFrr({"r1": ["lnk1", "lnk2"], "r2": ["lnk1", "lnk2"]})


def executor(frr: FakeFrr, options: FrrOptions = OPTIONS) -> FrrExecutor:
    clock = FakeClock()
    return FrrExecutor(frr, [L1, L2], options, sleep=clock.sleep, clock=clock)


def test_drain_costs_out_both_ends_and_waits_for_routes(frr: FakeFrr) -> None:
    ex = executor(frr)
    assert ex.routes_via(L1) == ["10.255.0.99/32"]
    ex.drain(L1, "CHG-1")
    assert frr.costs[("r1", "lnk1")] == frr.costs[("r2", "lnk1")] == 65535
    assert ex.routes_via(L1) == []
    assert ex.drained() == {L1}
    ex.undrain(L1, "CHG-2")
    assert frr.costs[("r1", "lnk1")] == frr.costs[("r2", "lnk1")] == 10
    assert ex.drained() == set()


def test_drain_is_reverted_when_routes_do_not_move(frr: FakeFrr) -> None:
    frr.frozen = True
    with pytest.raises(ExecutionError, match="still use lnk1 after 2s"):
        executor(frr).drain(L1, "CHG-1")
    assert frr.costs[("r1", "lnk1")] == frr.costs[("r2", "lnk1")] == 10


def test_drain_is_reverted_when_the_far_end_fails(frr: FakeFrr) -> None:
    frr.down.add("r2")
    with pytest.raises(ExecutionError, match="reverted"):
        executor(frr).drain(L1, "CHG-1")
    assert frr.costs[("r1", "lnk1")] == 10


def test_device_errors_surface_as_execution_errors(frr: FakeFrr) -> None:
    ex = executor(frr)
    frr.down.add("r1")
    with pytest.raises(ExecutionError, match="cannot read drain state"):
        ex.drained()
    with pytest.raises(ExecutionError, match="undrain"):
        ex.undrain(L1, "CHG-1")


def test_interface_without_ospf_is_an_error(frr: FakeFrr) -> None:
    ex = FrrExecutor(frr, [InterfaceRef("r1", "eth9")])
    with pytest.raises(ExecutionError, match="OSPF is not enabled"):
        ex.drained()


def test_engine_drives_frr_end_to_end(frr: FakeFrr, tmp_path) -> None:
    topology = Topology([LinkGroup("r1-r2", (Member(L1, 1), Member(L2, 1)))])
    settings = Settings(topology=topology, executor="frr", audit_path=tmp_path / "a.jsonl")
    engine = RemediationEngine(
        settings, executor=executor(frr), audit=AuditLog(tmp_path / "a.jsonl", fsync=False)
    )
    changes = []
    for minute in range(5):
        changes += engine.ingest(sample(L2, minute, 0.0, util=10)).changes
        changes += engine.ingest(sample(L1, minute, 25.0, util=10)).changes
    (change,) = changes
    assert change.action == Action.DRAIN and change.status == ChangeStatus.APPLIED
    assert change.checks[-1].detail == "device reports drained"
    assert frr.costs[("r2", "lnk1")] == 65535

    # Draining the only remaining link is refused before any device is touched.
    before = len(frr.commands)
    blocked = engine.request_change(Action.DRAIN, L2, "alice", "test", at(5))
    assert blocked.status == ChangeStatus.BLOCKED
    assert not any("configure terminal" in argv for _, argv in frr.commands[before:])

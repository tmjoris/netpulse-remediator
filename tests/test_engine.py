import pytest

from netpulse.audit import AuditLog
from netpulse.engine import ChangeConflict, RemediationEngine
from netpulse.executors import DryRunExecutor, LabExecutor
from netpulse.models import Action, ChangeStatus, IncidentState, Severity

from .conftest import AMS, LON, EngineFactory, at, feed


def changes_in(results: list) -> list:
    return [change for result in results for change in result.changes]


def audit_kinds(engine: RemediationEngine, kind: str) -> list[dict]:
    return [r for r in engine.audit.records() if r["kind"] == kind]


def test_full_lifecycle_drain_resolve_soak_undrain(make_engine: EngineFactory) -> None:
    engine = make_engine()
    drained_at = None
    for minute in range(5):
        changes = changes_in(feed(engine, minute, {LON[1]: 15}))
        if changes:
            drained_at = minute
            assert changes[0].action == Action.DRAIN and changes[0].status == ChangeStatus.APPLIED
    assert drained_at == 4
    incident = engine.incidents()[0]
    assert incident.state == IncidentState.MITIGATED
    assert incident.severity == Severity.CRITICAL
    assert LON[1] in engine.state.drained

    undrained_at = None
    for minute in range(5, 40):
        changes = changes_in(feed(engine, minute, {}))
        if changes:
            undrained_at = minute
            assert changes[0].action == Action.UNDRAIN and changes[0].status == ChangeStatus.APPLIED
    assert incident.state == IncidentState.RESOLVED
    # Healthy verdict at minute 8 (4 of 5 clear samples), plus the 15 minute soak.
    assert undrained_at == 23
    assert LON[1] not in engine.state.drained
    assert [e.kind for e in incident.events] == ["opened", "mitigated", "resolved", "undrained"]


def test_warning_opens_incident_without_draining(make_engine: EngineFactory) -> None:
    engine = make_engine()
    results = [r for minute in range(6) for r in feed(engine, minute, {LON[0]: 5})]
    assert not changes_in(results)
    (incident,) = engine.open_incidents()
    assert incident.severity == Severity.WARNING and incident.state == IncidentState.OPEN


def test_warning_escalates_to_critical(make_engine: EngineFactory) -> None:
    engine = make_engine()
    for minute in range(5):
        feed(engine, minute, {LON[0]: 5})
    for minute in range(5, 10):
        feed(engine, minute, {LON[0]: 30})
    (incident,) = engine.incidents()
    assert incident.severity == Severity.CRITICAL
    assert "severity" in [e.kind for e in incident.events]


def test_blocked_drain_is_escalated_and_recorded_once(make_engine: EngineFactory) -> None:
    engine = make_engine()
    results = [r for minute in range(15) for r in feed(engine, minute, {AMS[0]: 15}, util=60)]
    blocked = changes_in(results)
    assert len(blocked) == 1 and blocked[0].status == ChangeStatus.BLOCKED
    assert [c.name for c in blocked[0].failed_checks] == ["capacity_headroom"]
    assert engine.open_incidents()[0].state == IncidentState.ESCALATED
    assert len(audit_kinds(engine, "change")) == 1


def test_kill_switch_blocks_then_releases(make_engine: EngineFactory) -> None:
    engine = make_engine()
    engine.set_automation(False, "alice", "investigating bad telemetry", at(0))
    results = [r for minute in range(6) for r in feed(engine, minute, {LON[1]: 15})]
    (blocked,) = changes_in(results)
    assert [c.name for c in blocked.failed_checks] == ["kill_switch"]
    engine.set_automation(True, "alice", "telemetry fixed", at(6))
    (applied,) = changes_in(feed(engine, 6, {LON[1]: 15}))
    assert applied.status == ChangeStatus.APPLIED


def test_maintenance_window_suppresses_automation(make_engine: EngineFactory) -> None:
    engine = make_engine()
    window = engine.add_maintenance(LON[1].device, LON[1].interface, at(0), at(30), "optic swap", "bob")
    (blocked,) = changes_in([r for minute in range(10) for r in feed(engine, minute, {LON[1]: 15})])
    assert [c.name for c in blocked.failed_checks] == ["maintenance"]
    assert engine.remove_maintenance(window.id, "bob")
    assert not engine.remove_maintenance(window.id, "bob")
    with pytest.raises(ValueError, match="after start"):
        engine.add_maintenance("d", None, at(5), at(5), "zero length", "bob")


def test_operator_drain_is_never_auto_undrained(make_engine: EngineFactory) -> None:
    engine = make_engine()
    for minute in range(3):
        feed(engine, minute, {})
    change = engine.request_change(Action.DRAIN, LON[0], "alice", "planned fibre work", at(3))
    assert change.status == ChangeStatus.APPLIED
    assert not engine.state.drained[LON[0]].automated
    results = [r for minute in range(4, 60) for r in feed(engine, minute, {})]
    assert not changes_in(results)
    undrain = engine.request_change(Action.UNDRAIN, LON[0], "alice", "work complete", at(60))
    assert undrain.status == ChangeStatus.APPLIED and LON[0] not in engine.state.drained


def test_operator_drain_still_checks_capacity(make_engine: EngineFactory) -> None:
    engine = make_engine()
    for minute in range(3):
        feed(engine, minute, {}, util=60)
    change = engine.request_change(Action.DRAIN, AMS[0], "alice", "maintenance", at(3))
    assert change.status == ChangeStatus.BLOCKED


def test_request_change_conflicts(make_engine: EngineFactory) -> None:
    engine = make_engine()
    with pytest.raises(ChangeConflict, match="reserved"):
        engine.request_change(Action.DRAIN, LON[0], "netpulse", "pretending", at(0))
    with pytest.raises(ChangeConflict, match="not drained"):
        engine.request_change(Action.UNDRAIN, LON[0], "alice", "nothing to do", at(0))
    feed(engine, 0, {})
    engine.request_change(Action.DRAIN, LON[0], "alice", "work", at(0))
    with pytest.raises(ChangeConflict, match="already drained"):
        engine.request_change(Action.DRAIN, LON[0], "alice", "again", at(1))


def test_executor_failure_escalates_and_retries_after_cooldown(make_engine: EngineFactory) -> None:
    executor = LabExecutor()
    executor.fail_on.add((Action.DRAIN, LON[1]))
    engine = make_engine(executor)
    results = [r for minute in range(20) for r in feed(engine, minute, {LON[1]: 15})]
    statuses = [c.status for c in changes_in(results)]
    assert statuses == [ChangeStatus.FAILED, ChangeStatus.BLOCKED, ChangeStatus.APPLIED]
    failed = changes_in(results)[0]
    assert "injected failure" in failed.failed_checks[0].detail
    assert engine.incidents()[0].state == IncidentState.MITIGATED


def test_post_check_failure_rolls_back(make_engine: EngineFactory) -> None:
    executor = LabExecutor()
    executor.ignore_on.add((Action.DRAIN, LON[1]))
    engine = make_engine(executor)
    results = [r for minute in range(5) for r in feed(engine, minute, {LON[1]: 15})]
    (change,) = changes_in(results)
    assert change.status == ChangeStatus.ROLLED_BACK
    assert [c.name for c in change.checks][-2:] == ["post_check", "rollback"]
    assert LON[1] not in engine.state.drained


def test_flap_damping_holds_drain_for_a_human(make_engine: EngineFactory) -> None:
    engine = make_engine()
    minute = 0
    for _ in range(3):
        for _ in range(6):
            feed(engine, minute, {LON[3]: 20})
            minute += 1
        for _ in range(30):
            feed(engine, minute, {})
            minute += 1
    assert engine.state.drained[LON[3]].hold_reason is not None
    assert "flap_damping" in (engine.state.drained[LON[3]].hold_reason or "")
    assert len(engine.state.drain_history[LON[3]]) == 3


def test_state_survives_restart(make_engine: EngineFactory, settings) -> None:
    engine = make_engine()
    for minute in range(5):
        feed(engine, minute, {LON[1]: 15})
    engine.set_automation(False, "alice", "change freeze", at(5))
    window = engine.add_maintenance("edge-dub-02", None, at(0), at(600), "upgrade", "bob")

    restarted = RemediationEngine(
        settings, executor=LabExecutor({LON[1]}), audit=AuditLog(settings.audit_path, fsync=False)
    )
    assert restarted.state.automation_enabled is False
    assert restarted.state.automation_actor == "alice"
    assert restarted.state.drained[LON[1]].automated
    assert restarted.state.drain_history[LON[1]] == [at(4)]
    assert window.id in restarted.state.maintenance
    for minute in range(5, 10):
        feed(restarted, minute, {LON[2]: 15})
    assert restarted.incidents()[0].id == "INC-000002"
    assert [r["data"]["id"] for r in audit_kinds(restarted, "change")][-1] == "CHG-000002"


def test_reconcile_trusts_the_executor(make_engine: EngineFactory, settings) -> None:
    engine = make_engine()
    for minute in range(5):
        feed(engine, minute, {LON[1]: 15})
    # The device no longer reports LON[1] drained, and LON[2] was drained by hand on the box.
    restarted = RemediationEngine(
        settings, executor=LabExecutor({LON[2]}), audit=AuditLog(settings.audit_path, fsync=False)
    )
    assert LON[1] not in restarted.state.drained
    assert restarted.state.drained[LON[2]].actor == "unknown"
    assert len(audit_kinds(restarted, "reconcile")) == 2


def test_dry_run_records_shadow_state(make_engine: EngineFactory) -> None:
    engine = make_engine(DryRunExecutor())
    results = [r for minute in range(5) for r in feed(engine, minute, {LON[1]: 15})]
    (change,) = changes_in(results)
    assert change.status == ChangeStatus.DRY_RUN
    assert LON[1] in engine.state.drained


def test_out_of_order_sample_is_counted_not_evaluated(make_engine: EngineFactory) -> None:
    engine = make_engine()
    feed(engine, 5, {})
    (result,) = [r for r in feed(engine, 4, {})][:1]
    assert not result.accepted and "not newer" in (result.error or "")


def test_interface_status_and_incident_queries(make_engine: EngineFactory) -> None:
    engine = make_engine()
    for minute in range(5):
        feed(engine, minute, {LON[1]: 15})
    rows = {row["target"]: row for row in engine.interface_status()}
    assert rows[str(LON[1])]["drained"]["actor"] == "netpulse"
    assert rows[str(LON[1])]["link_group"] == "dub-lon-bundle"
    incident = engine.incidents(IncidentState.MITIGATED)[0]
    assert engine.incident(incident.id) is incident
    assert engine.incidents(IncidentState.RESOLVED) == []


def test_resolved_incidents_are_evicted_beyond_retention(settings) -> None:
    engine = RemediationEngine(
        settings,
        executor=LabExecutor(),
        audit=AuditLog(settings.audit_path, fsync=False),
        incident_retention=2,
    )
    minute = 0
    for _ in range(4):
        for _ in range(5):
            feed(engine, minute, {LON[0]: 5})
            minute += 1
        for _ in range(5):
            feed(engine, minute, {})
            minute += 1
    assert len(engine.incidents()) == 2

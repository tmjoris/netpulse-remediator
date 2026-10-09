from datetime import timedelta

from netpulse.config import SafetyPolicy
from netpulse.models import CheckResult, InterfaceRef, MaintenanceWindow, Sample
from netpulse.safety import ControlState, DrainInfo, SafetyChecker, human
from netpulse.simulator import LAB_TOPOLOGY

from .conftest import AMS, LON, at, sample


def latest_from(samples: dict[InterfaceRef, Sample]):
    return samples.get


def by_name(checks: list[CheckResult]) -> dict[str, CheckResult]:
    return {check.name: check for check in checks}


def test_capacity_headroom_projects_load_onto_survivors() -> None:
    checker = SafetyChecker(SafetyPolicy(), LAB_TOPOLOGY)
    samples = {ref: sample(ref, 10, util=40) for ref in LON}  # 160 Gbps over 4x100G
    checks = by_name(checker.drain_checks(LON[0], at(10), ControlState(), samples.get))
    assert checks["capacity_headroom"].passed
    assert "projected 53%" in checks["capacity_headroom"].detail


def test_capacity_headroom_blocks_overload() -> None:
    checker = SafetyChecker(SafetyPolicy(), LAB_TOPOLOGY)
    samples = {ref: sample(ref, 10, util=60) for ref in AMS}  # 120 Gbps over 2x100G
    checks = by_name(checker.drain_checks(AMS[0], at(10), ControlState(), samples.get))
    assert not checks["capacity_headroom"].passed
    assert "projected 120%" in checks["capacity_headroom"].detail


def test_recently_drained_member_traffic_is_still_counted() -> None:
    # LON[0] was drained in this polling cycle, its last sample still shows 40%.
    checker = SafetyChecker(SafetyPolicy(), LAB_TOPOLOGY)
    state = ControlState(drained={LON[0]: DrainInfo(at(10), "netpulse", "CHG-1")})
    samples = {ref: sample(ref, 10, util=40) for ref in LON}
    checks = by_name(checker.drain_checks(LON[1], at(10), state, samples.get))
    assert "160/200 Gbps" in checks["capacity_headroom"].detail


def test_fails_closed_on_stale_or_missing_utilization() -> None:
    checker = SafetyChecker(SafetyPolicy(utilization_max_age=timedelta(minutes=5)), LAB_TOPOLOGY)
    samples = {ref: sample(ref, 0, util=10) for ref in LON[:3]}  # LON[3] missing, others old
    checks = by_name(checker.drain_checks(LON[0], at(10), ControlState(), samples.get))
    assert not checks["capacity_headroom"].passed
    assert "no fresh utilization" in checks["capacity_headroom"].detail


def test_unknown_interface_is_never_drained_automatically() -> None:
    checker = SafetyChecker(SafetyPolicy(), LAB_TOPOLOGY)
    ref = InterfaceRef("core-01", "ae0")
    checks = by_name(checker.drain_checks(ref, at(0), ControlState(), {}.get))
    assert not checks["topology"].passed


def test_automation_guards() -> None:
    checker = SafetyChecker(SafetyPolicy(max_concurrent_drains=1), LAB_TOPOLOGY)
    window = MaintenanceWindow("MW-1", LON[0].device, None, at(0), at(60), "optic swap", "alice")
    state = ControlState(
        automation_enabled=False,
        automation_reason="bad deploy",
        drained={AMS[0]: DrainInfo(at(0), "netpulse", "CHG-1")},
        last_auto_action={LON[0]: at(5)},
        maintenance={window.id: window},
    )
    samples = {ref: sample(ref, 10) for ref in LON}
    checks = by_name(checker.drain_checks(LON[0], at(10), state, samples.get))
    assert not checks["kill_switch"].passed and "bad deploy" in checks["kill_switch"].detail
    assert not checks["maintenance"].passed
    assert not checks["cooldown"].passed
    assert not checks["fleet_concurrency"].passed
    # Operators bypass the automation guards but not the redundancy checks.
    manual = by_name(checker.drain_checks(LON[0], at(10), state, samples.get, automated=False))
    assert set(manual) == {"topology", "min_active_members", "capacity_headroom"}


def test_operator_drains_do_not_count_toward_fleet_limit() -> None:
    checker = SafetyChecker(SafetyPolicy(max_concurrent_drains=1), LAB_TOPOLOGY)
    state = ControlState(drained={AMS[0]: DrainInfo(at(0), "alice", "CHG-1")})
    samples = {ref: sample(ref, 10) for ref in LON}
    assert by_name(checker.drain_checks(LON[0], at(10), state, samples.get))["fleet_concurrency"].passed


def test_undrain_requires_soak_and_respects_flap_damping() -> None:
    checker = SafetyChecker(SafetyPolicy(undrain_soak=timedelta(minutes=15), flap_max_drains=2), LAB_TOPOLOGY)
    state = ControlState(healthy_since={LON[0]: at(0)})
    assert not by_name(checker.undrain_checks(LON[0], at(10), state))["soak"].passed
    assert by_name(checker.undrain_checks(LON[0], at(15), state))["soak"].passed
    state.drain_history[LON[0]] = [at(-60), at(-30)]
    assert not by_name(checker.undrain_checks(LON[0], at(15), state))["flap_damping"].passed
    assert all(c.passed for c in checker.undrain_checks(LON[0], at(15), state, automated=False))


def test_human_durations() -> None:
    assert human(timedelta(hours=24)) == "24h"
    assert human(timedelta(minutes=9, seconds=30)) == "9m30s"
    assert human(timedelta(0)) == "0s"

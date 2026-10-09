"""Each lab scenario encodes an operational expectation; these tests pin them."""

from netpulse.simulator import AMS, LON, SCENARIOS, InProcessSink, SimulationReport, lab_engine, run_scenario


def run(name: str) -> SimulationReport:
    scenario = SCENARIOS[name]
    return run_scenario(scenario, InProcessSink(lab_engine(scenario)))


def failed_checks(change: dict) -> set[str]:
    return {check["name"] for check in change["checks"] if not check["passed"]}


def test_gray_failure_is_drained_then_undrained() -> None:
    report = run("gray-failure")
    assert [c["target"] for c in report.changes("drain", "applied")] == [str(LON[1])]
    assert len(report.changes("undrain", "applied")) == 1
    assert report.drained_at_end == set()
    assert report.count("resolved") == 1


def test_insufficient_capacity_escalates_instead_of_draining() -> None:
    report = run("insufficient-capacity")
    assert report.changes("drain", "applied") == []
    (blocked,) = report.changes("drain", "blocked")
    assert blocked["target"] == str(AMS[0])
    assert failed_checks(blocked) == {"capacity_headroom"}
    assert report.count("escalated") == 1


def test_correlated_failure_respects_blast_radius() -> None:
    report = run("correlated-failure")
    drained = [c["target"] for c in report.changes("drain", "applied")]
    assert drained == [str(LON[0]), str(LON[1])]
    blocked = report.changes("drain", "blocked")
    assert blocked and all(c["target"] == str(LON[2]) for c in blocked)
    assert "fleet_concurrency" in failed_checks(blocked[-1])
    assert len(report.changes("undrain", "applied")) == 2
    assert report.drained_at_end == set()


def test_flapping_link_is_held_drained() -> None:
    report = run("flapping")
    assert len(report.changes("drain", "applied")) == 3
    assert len(report.changes("undrain", "applied")) == 2
    (held,) = report.changes("undrain", "blocked")
    assert failed_checks(held) == {"flap_damping"}
    assert report.drained_at_end == {LON[3]}


def test_change_verification_rolls_back_then_retries() -> None:
    report = run("change-verification")
    statuses = [c["status"] for c in report.changes("drain")]
    assert statuses[0] == "rolled_back"
    assert statuses[-1] == "applied"
    assert report.drained_at_end == set()


def test_scenarios_are_deterministic() -> None:
    first, second = run("correlated-failure"), run("correlated-failure")
    strip = [{k: v for k, v in r.items() if k != "logged_at"} for r in first.records]
    assert strip == [{k: v for k, v in r.items() if k != "logged_at"} for r in second.records]

from datetime import datetime, timedelta, timezone

from netpulse.engine import RemediationEngine
from netpulse.models import Action, Sample, Severity


def make_sample(index: int, loss: float, latency: float) -> Sample:
    return Sample("r1", "eth0", datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
                  latency, loss, 40)


def test_ignores_single_noisy_sample() -> None:
    engine = RemediationEngine(window=3)
    assert engine.evaluate(make_sample(0, 20, 300)) is None


def test_sustained_warning_proposes_traffic_shift() -> None:
    engine = RemediationEngine(window=3)
    incidents = [engine.evaluate(make_sample(i, 4, 120)) for i in range(3)]
    assert incidents[-1].severity == Severity.WARNING
    assert incidents[-1].action == Action.SHIFT_TRAFFIC


def test_critical_incident_is_cooldown_protected() -> None:
    engine = RemediationEngine(window=2, cooldown=timedelta(minutes=10))
    first = engine.evaluate(make_sample(0, 15, 200))
    second = engine.evaluate(make_sample(1, 15, 200))
    third = engine.evaluate(make_sample(2, 15, 200))
    assert first is None
    assert second.action == Action.DISABLE_INTERFACE
    assert third.action == Action.NOOP


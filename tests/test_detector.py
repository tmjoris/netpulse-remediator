import pytest

from netpulse.config import DetectionPolicy
from netpulse.detector import Detector, OutOfOrderSample
from netpulse.models import Verdict

from .conftest import LON, sample

REF = LON[0]


def run(detector: Detector, losses: list[float], start: int = 0) -> Verdict:
    verdict = Verdict.INSUFFICIENT_DATA
    for offset, loss in enumerate(losses):
        verdict = detector.observe(sample(REF, start + offset, loss)).verdict
    return verdict


def test_needs_a_full_window_before_judging() -> None:
    detector = Detector(DetectionPolicy(window=5, breach_count=4))
    assert run(detector, [50, 50, 50, 50]) == Verdict.INSUFFICIENT_DATA


def test_single_spike_does_not_raise() -> None:
    detector = Detector(DetectionPolicy(window=5, breach_count=4))
    assert run(detector, [0, 0, 60, 0, 0]) == Verdict.HEALTHY


def test_m_of_n_breaches_raise_critical() -> None:
    detector = Detector(DetectionPolicy(window=5, breach_count=4))
    assert run(detector, [0, 15, 15, 15, 15]) == Verdict.CRITICAL


def test_warning_from_latency_alone() -> None:
    detector = Detector(DetectionPolicy(window=3, breach_count=3))
    for minute in range(3):
        evaluation = detector.observe(sample(REF, minute, loss=0.0, latency=150))
    assert evaluation.verdict == Verdict.WARNING


def test_hysteresis_holds_between_clear_and_raise_thresholds() -> None:
    # 1% loss is below the 2% warning threshold but above the 0.5% clear threshold.
    detector = Detector(DetectionPolicy(window=5, breach_count=4))
    assert run(detector, [1, 1, 1, 1, 1]) == Verdict.HOLD


def test_out_of_order_and_duplicate_samples_are_rejected() -> None:
    detector = Detector(DetectionPolicy())
    detector.observe(sample(REF, 5))
    with pytest.raises(OutOfOrderSample):
        detector.observe(sample(REF, 5))
    with pytest.raises(OutOfOrderSample):
        detector.observe(sample(REF, 4))
    assert detector.latest(REF) is not None
    assert detector.latest(REF).timestamp == sample(REF, 5).timestamp  # type: ignore[union-attr]

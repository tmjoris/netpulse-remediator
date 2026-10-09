"""Per-interface sliding-window detection with M-of-N breach rules and hysteresis."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .config import DetectionPolicy
from .models import InterfaceRef, Sample, Verdict


class OutOfOrderSample(ValueError):
    """Raised for a sample that is not newer than the last one seen for its interface."""


@dataclass(frozen=True)
class Evaluation:
    verdict: Verdict
    samples: int
    critical_breaches: int
    breaches: int
    clear: int
    mean_loss_pct: float
    mean_latency_ms: float

    def describe(self, breach_count: int) -> str:
        return (
            f"{self.breaches}/{self.samples} samples breaching "
            f"({self.critical_breaches} critical, need {breach_count}); "
            f"mean loss {self.mean_loss_pct:.2f}%, mean latency {self.mean_latency_ms:.1f}ms"
        )


class Detector:
    """Classify each interface from its most recent samples.

    A single noisy sample never changes state: ``breach_count`` of the last
    ``window`` samples must breach to raise, and the same number must be below
    the (lower) clear thresholds to resolve. Anything in between is ``HOLD``.
    """

    def __init__(self, policy: DetectionPolicy) -> None:
        self.policy = policy
        self._windows: dict[InterfaceRef, deque[Sample]] = {}

    def observe(self, sample: Sample) -> Evaluation:
        window = self._windows.setdefault(sample.ref, deque(maxlen=self.policy.window))
        if window and sample.timestamp <= window[-1].timestamp:
            raise OutOfOrderSample(
                f"{sample.ref}: sample at {sample.timestamp.isoformat()} is not newer than "
                f"{window[-1].timestamp.isoformat()}"
            )
        window.append(sample)
        return self._evaluate(window)

    def latest(self, ref: InterfaceRef) -> Sample | None:
        window = self._windows.get(ref)
        return window[-1] if window else None

    def interfaces(self) -> list[InterfaceRef]:
        return sorted(self._windows)

    def _evaluate(self, window: deque[Sample]) -> Evaluation:
        p = self.policy
        critical = sum(s.packet_loss_pct >= p.loss_critical_pct for s in window)
        breaches = sum(
            s.packet_loss_pct >= p.loss_warning_pct or s.latency_ms >= p.latency_warning_ms for s in window
        )
        clear = sum(
            s.packet_loss_pct < p.loss_clear_pct and s.latency_ms < p.latency_clear_ms for s in window
        )
        if len(window) < p.window:
            verdict = Verdict.INSUFFICIENT_DATA
        elif critical >= p.breach_count:
            verdict = Verdict.CRITICAL
        elif breaches >= p.breach_count:
            verdict = Verdict.WARNING
        elif clear >= p.breach_count:
            verdict = Verdict.HEALTHY
        else:
            verdict = Verdict.HOLD
        return Evaluation(
            verdict=verdict,
            samples=len(window),
            critical_breaches=critical,
            breaches=breaches,
            clear=clear,
            mean_loss_pct=sum(s.packet_loss_pct for s in window) / len(window),
            mean_latency_ms=sum(s.latency_ms for s in window) / len(window),
        )

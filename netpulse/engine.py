from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Iterable

from .models import Action, Incident, Sample, Severity


class RemediationEngine:
    """Evaluate sustained interface degradation and create safe plans."""

    def __init__(
        self,
        *,
        window: int = 5,
        latency_warning_ms: float = 100,
        loss_warning_pct: float = 2,
        loss_critical_pct: float = 10,
        cooldown: timedelta = timedelta(minutes=10),
    ) -> None:
        if window < 2:
            raise ValueError("window must be at least 2")
        self.window = window
        self.latency_warning_ms = latency_warning_ms
        self.loss_warning_pct = loss_warning_pct
        self.loss_critical_pct = loss_critical_pct
        self.cooldown = cooldown
        self._samples: dict[str, deque[Sample]] = defaultdict(lambda: deque(maxlen=window))
        self._last_actions: dict[str, datetime] = {}

    def evaluate(self, sample: Sample) -> Incident | None:
        key = f"{sample.device}:{sample.interface}"
        history = self._samples[key]
        history.append(sample)
        if len(history) < self.window:
            return None

        loss = sum(item.packet_loss_pct for item in history) / len(history)
        latency = sum(item.latency_ms for item in history) / len(history)
        if loss < self.loss_warning_pct and latency < self.latency_warning_ms:
            return None

        critical = loss >= self.loss_critical_pct
        severity = Severity.CRITICAL if critical else Severity.WARNING
        action = Action.DISABLE_INTERFACE if critical else Action.SHIFT_TRAFFIC
        reason = f"{len(history)}-sample average: loss={loss:.2f}%, latency={latency:.1f}ms"
        last_action = self._last_actions.get(key)
        if last_action and sample.timestamp - last_action < self.cooldown:
            action = Action.NOOP
            reason += "; remediation suppressed by cooldown"
        else:
            self._last_actions[key] = sample.timestamp

        return Incident(
            key=key,
            severity=severity,
            reason=reason,
            action=action,
            first_seen=history[0].timestamp,
            last_seen=sample.timestamp,
            sample_count=len(history),
            metadata={"device": sample.device, "interface": sample.interface},
        )

    def evaluate_many(self, samples: Iterable[Sample]) -> list[Incident]:
        return [incident for sample in samples if (incident := self.evaluate(sample))]


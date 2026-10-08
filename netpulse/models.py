from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class Action(StrEnum):
    NOOP = "noop"
    SHIFT_TRAFFIC = "shift_traffic"
    DISABLE_INTERFACE = "disable_interface"


@dataclass(frozen=True)
class Sample:
    device: str
    interface: str
    timestamp: datetime
    latency_ms: float
    packet_loss_pct: float
    utilization_pct: float

    @classmethod
    def now(cls, device: str, interface: str, **values: float) -> "Sample":
        return cls(device, interface, datetime.now(timezone.utc), **values)


@dataclass
class Incident:
    key: str
    severity: Severity
    reason: str
    action: Action
    first_seen: datetime
    last_seen: datetime
    sample_count: int
    metadata: dict[str, str] = field(default_factory=dict)


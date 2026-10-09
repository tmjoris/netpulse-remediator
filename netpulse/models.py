"""Domain types shared by the detector, engine, executors and API."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class Verdict(StrEnum):
    """Result of evaluating one interface's recent sample window."""

    INSUFFICIENT_DATA = "insufficient_data"
    HEALTHY = "healthy"
    HOLD = "hold"  # between clear and breach thresholds: keep the current state
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentState(StrEnum):
    OPEN = "open"  # detected, no change applied yet
    MITIGATED = "mitigated"  # interface drained
    ESCALATED = "escalated"  # automation refused or failed to act; a human must decide
    RESOLVED = "resolved"


class Action(StrEnum):
    DRAIN = "drain"
    UNDRAIN = "undrain"


class ChangeStatus(StrEnum):
    APPLIED = "applied"
    DRY_RUN = "dry_run"
    BLOCKED = "blocked"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, order=True)
class InterfaceRef:
    device: str
    interface: str

    def __str__(self) -> str:
        return f"{self.device}:{self.interface}"

    @classmethod
    def parse(cls, value: str) -> InterfaceRef:
        device, sep, interface = value.partition(":")
        if not sep or not device or not interface:
            raise ValueError(f"expected 'device:interface', got {value!r}")
        return cls(device, interface)


@dataclass(frozen=True)
class Sample:
    device: str
    interface: str
    timestamp: datetime
    latency_ms: float
    packet_loss_pct: float
    utilization_pct: float

    @property
    def ref(self) -> InterfaceRef:
        return InterfaceRef(self.device, self.interface)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class ChangeRecord:
    id: str
    action: Action
    target: InterfaceRef
    status: ChangeStatus
    actor: str
    reason: str
    requested_at: datetime
    incident_id: str | None = None
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def effective(self) -> bool:
        """True when the change is now part of the (real or shadow) network state."""
        return self.status in (ChangeStatus.APPLIED, ChangeStatus.DRY_RUN)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]


@dataclass(frozen=True)
class IncidentEvent:
    at: datetime
    kind: str
    detail: str


@dataclass
class Incident:
    id: str
    target: InterfaceRef
    severity: Severity
    state: IncidentState
    opened_at: datetime
    updated_at: datetime
    reason: str
    resolved_at: datetime | None = None
    mitigated_at: datetime | None = None
    change_ids: list[str] = field(default_factory=list)
    events: list[IncidentEvent] = field(default_factory=list)

    def record(self, at: datetime, kind: str, detail: str) -> None:
        self.updated_at = at
        self.events.append(IncidentEvent(at, kind, detail))


@dataclass(frozen=True)
class MaintenanceWindow:
    id: str
    device: str
    interface: str | None  # None covers every interface on the device
    start: datetime
    end: datetime
    reason: str
    actor: str

    def covers(self, ref: InterfaceRef, at: datetime) -> bool:
        if ref.device != self.device:
            return False
        if self.interface is not None and ref.interface != self.interface:
            return False
        return self.start <= at < self.end


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_jsonable(value: Any) -> Any:
    """Convert domain objects into JSON-compatible structures."""
    if isinstance(value, InterfaceRef):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value

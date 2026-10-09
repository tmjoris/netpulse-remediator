"""Incident lifecycle and remediation orchestration.

    telemetry -> Detector -> incident lifecycle -> SafetyChecker -> Executor
                                                                    |
                    AuditLog <- change record + post-check/rollback <+

The engine is not thread-safe; callers (the HTTP server) serialize access.
Policy decisions use the sample's event time, so replayed or simulated
telemetry behaves exactly like live telemetry.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any

from .audit import AuditLog
from .config import Settings
from .detector import Detector, Evaluation, OutOfOrderSample
from .executors import Executor, build_executor
from .metrics import Metrics
from .models import (
    Action,
    ChangeRecord,
    ChangeStatus,
    CheckResult,
    Incident,
    IncidentState,
    InterfaceRef,
    MaintenanceWindow,
    Sample,
    Severity,
    Verdict,
    to_jsonable,
    utcnow,
)
from .safety import AUTOMATION_ACTOR, ControlState, DrainInfo, SafetyChecker, summarize

log = logging.getLogger("netpulse.engine")

# Undrain checks that mean "not yet" rather than "no": they are re-evaluated on
# every healthy sample without filling the audit log with blocked attempts.
_WAITING_CHECKS = frozenset({"soak", "cooldown"})


class ChangeConflict(ValueError):
    """A requested change does not make sense for the current state."""


@dataclass(frozen=True)
class EngineEvent:
    at: datetime
    kind: str
    target: InterfaceRef
    detail: str
    incident_id: str | None = None
    change_id: str | None = None


@dataclass
class IngestResult:
    accepted: bool
    target: InterfaceRef
    verdict: Verdict | None = None
    error: str | None = None
    incident: Incident | None = None
    changes: list[ChangeRecord] = field(default_factory=list)
    events: list[EngineEvent] = field(default_factory=list)


@dataclass
class _Replayed:
    state: ControlState
    change_seq: int = 0
    incident_seq: int = 0
    maintenance_seq: int = 0


def _seq(identifier: str | None) -> int:
    try:
        return int(str(identifier).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def replay(records: Iterable[dict[str, Any]], automation_enabled: bool) -> _Replayed:
    """Rebuild control-plane state from audit records.

    This is what stops a restart from silently re-enabling automation that an
    operator turned off, or forgetting which drains automation owns.
    """
    out = _Replayed(ControlState(automation_enabled=automation_enabled))
    state = out.state
    for record in records:
        kind, data = record["kind"], record["data"]
        at = datetime.fromisoformat(record["at"])
        if kind == "change":
            out.change_seq = max(out.change_seq, _seq(data["id"]))
            ref = InterfaceRef.parse(data["target"])
            automated = data["actor"] == AUTOMATION_ACTOR
            status = ChangeStatus(data["status"])
            if automated and status != ChangeStatus.BLOCKED:
                state.last_auto_action[ref] = at
            if status in (ChangeStatus.APPLIED, ChangeStatus.DRY_RUN):
                if data["action"] == Action.DRAIN:
                    state.drained[ref] = DrainInfo(at, data["actor"], data["id"], data.get("incident_id"))
                    if automated:
                        state.drain_history.setdefault(ref, []).append(at)
                else:
                    state.drained.pop(ref, None)
            elif status == ChangeStatus.BLOCKED and data["action"] == Action.UNDRAIN and ref in state.drained:
                failed = [c for c in data["checks"] if not c["passed"]]
                state.drained[ref].hold_reason = "; ".join(f"{c['name']}: {c['detail']}" for c in failed)
        elif kind == "automation":
            state.automation_enabled = data["enabled"]
            state.automation_reason = data["reason"]
            state.automation_actor = data["actor"]
        elif kind == "maintenance.add":
            window = MaintenanceWindow(
                id=data["id"],
                device=data["device"],
                interface=data["interface"],
                start=datetime.fromisoformat(data["start"]),
                end=datetime.fromisoformat(data["end"]),
                reason=data["reason"],
                actor=data["actor"],
            )
            state.maintenance[window.id] = window
            out.maintenance_seq = max(out.maintenance_seq, _seq(window.id))
        elif kind == "maintenance.remove":
            state.maintenance.pop(data["id"], None)
        elif kind == "event":
            out.incident_seq = max(out.incident_seq, _seq(data.get("incident_id")))
        elif kind == "reconcile":
            ref = InterfaceRef.parse(data["target"])
            if data["drained"]:
                state.drained.setdefault(ref, DrainInfo(at, data["actor"], "reconcile"))
            else:
                state.drained.pop(ref, None)
    return out


class RemediationEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        executor: Executor | None = None,
        audit: AuditLog | None = None,
        metrics: Metrics | None = None,
        incident_retention: int = 1000,
    ) -> None:
        self.settings = settings or Settings()
        self.detector = Detector(self.settings.detection)
        self.safety = SafetyChecker(self.settings.safety, self.settings.topology)
        self.audit = audit or AuditLog(self.settings.audit_path)
        replayed = replay(self.audit.records(), self.settings.safety.automation_enabled)
        self.state = replayed.state
        self._change_seq = replayed.change_seq
        self._incident_seq = replayed.incident_seq
        self._maintenance_seq = replayed.maintenance_seq
        self.executor = executor or build_executor(self.settings, self.state.drained)
        self.metrics = metrics or Metrics()
        self.metrics.bind(self)
        self._incidents: OrderedDict[str, Incident] = OrderedDict()
        self._open: dict[InterfaceRef, str] = {}
        self._retention = incident_retention
        self._last_block: dict[tuple[Action, InterfaceRef], tuple[str, ...]] = {}
        self._events: list[EngineEvent] = []
        self._reconcile(utcnow())

    # -- telemetry -----------------------------------------------------------

    def ingest(self, sample: Sample) -> IngestResult:
        started = perf_counter()
        self._events = []
        try:
            return self._ingest(sample)
        finally:
            self.metrics.evaluation_seconds.observe(perf_counter() - started)

    def _ingest(self, sample: Sample) -> IngestResult:
        try:
            evaluation = self.detector.observe(sample)
        except OutOfOrderSample as exc:
            self.metrics.samples.labels("out_of_order").inc()
            return IngestResult(False, sample.ref, error=str(exc))
        self.metrics.samples.labels("accepted").inc()

        ref, now, verdict = sample.ref, sample.timestamp, evaluation.verdict
        if verdict == Verdict.HEALTHY:
            self.state.healthy_since.setdefault(ref, now)
        elif verdict != Verdict.INSUFFICIENT_DATA:
            self.state.healthy_since.pop(ref, None)

        changes: list[ChangeRecord] = []
        incident = self._open_incident(ref)
        if verdict in (Verdict.WARNING, Verdict.CRITICAL):
            incident = self._raise(ref, evaluation, now, incident)
            drainable = verdict == Verdict.CRITICAL and ref not in self.state.drained
            if drainable and (change := self._auto_drain(incident, now)):
                changes.append(change)
        elif verdict == Verdict.HEALTHY:
            if incident is not None:
                self._resolve(incident, now, evaluation)
            info = self.state.drained.get(ref)
            if info is not None and info.automated and (change := self._auto_undrain(ref, info, now)):
                changes.append(change)
        return IngestResult(True, ref, verdict, None, incident, changes, self._events)

    def _raise(
        self, ref: InterfaceRef, evaluation: Evaluation, now: datetime, incident: Incident | None
    ) -> Incident:
        severity = Severity(evaluation.verdict.value)
        reason = evaluation.describe(self.settings.detection.breach_count)
        if incident is None:
            self._incident_seq += 1
            incident = Incident(
                id=f"INC-{self._incident_seq:06d}",
                target=ref,
                severity=severity,
                state=IncidentState.OPEN,
                opened_at=now,
                updated_at=now,
                reason=reason,
            )
            self._store(incident)
            self._last_block.pop((Action.DRAIN, ref), None)
            self.metrics.incidents_opened.labels(severity.value).inc()
            self._emit(incident, now, "opened", f"{severity}: {reason}")
            if ref in self.state.drained:
                incident.state = IncidentState.MITIGATED
                incident.mitigated_at = now
                self._emit(incident, now, "mitigated", "interface is already drained")
        elif severity == Severity.CRITICAL and incident.severity == Severity.WARNING:
            incident.severity = Severity.CRITICAL
            self._emit(incident, now, "severity", f"escalated to critical: {reason}")
        incident.reason = reason
        return incident

    def _resolve(self, incident: Incident, now: datetime, evaluation: Evaluation) -> None:
        incident.state = IncidentState.RESOLVED
        incident.resolved_at = now
        self._open.pop(incident.target, None)
        self.metrics.incidents_resolved.inc()
        detail = f"healthy: {evaluation.clear}/{evaluation.samples} samples below clear thresholds"
        if incident.target in self.state.drained:
            detail += "; interface stays drained until the undrain soak passes"
        self._emit(incident, now, "resolved", detail)

    # -- automated remediation -------------------------------------------------

    def _auto_drain(self, incident: Incident, now: datetime) -> ChangeRecord | None:
        ref = incident.target
        checks = self.safety.drain_checks(ref, now, self.state, self.detector.latest)
        reason = f"critical incident {incident.id}: {incident.reason}"
        if not all(check.passed for check in checks):
            if not self._first_block(Action.DRAIN, ref, checks):
                return None
            change = self._new_change(Action.DRAIN, ref, AUTOMATION_ACTOR, reason, now, incident.id, checks)
            self._record_change(change)
            incident.state = IncidentState.ESCALATED
            self._emit(incident, now, "escalated", f"drain blocked: {summarize(checks)}", change.id)
            return change

        change = self._new_change(Action.DRAIN, ref, AUTOMATION_ACTOR, reason, now, incident.id, checks)
        self._execute(change, now)
        self._after_drain(incident, change, now)
        return change

    def _auto_undrain(self, ref: InterfaceRef, info: DrainInfo, now: datetime) -> ChangeRecord | None:
        checks = self.safety.undrain_checks(ref, now, self.state)
        failed = [check for check in checks if not check.passed]
        if any(check.name in _WAITING_CHECKS for check in failed):
            return None
        incident = self._incidents.get(info.incident_id or "")
        reason = f"healthy after drain {info.change_id}"
        if failed:
            info.hold_reason = summarize(checks)
            if not self._first_block(Action.UNDRAIN, ref, checks):
                return None
            change = self._new_change(
                Action.UNDRAIN, ref, AUTOMATION_ACTOR, reason, now, info.incident_id, checks
            )
            self._record_change(change)
            self._emit(incident, now, "held", f"undrain blocked: {info.hold_reason}", change.id, ref)
            return change

        change = self._new_change(
            Action.UNDRAIN, ref, AUTOMATION_ACTOR, reason, now, info.incident_id, checks
        )
        self._execute(change, now)
        detail = f"undrain {change.status}"
        if not change.effective:
            detail += f": {summarize(change.checks)}"
        self._emit(incident, now, "undrained" if change.effective else "held", detail, change.id, ref)
        return change

    def _after_drain(self, incident: Incident, change: ChangeRecord, now: datetime) -> None:
        if change.effective:
            incident.state = IncidentState.MITIGATED
            incident.mitigated_at = now
            self.metrics.time_to_mitigate.observe((now - incident.opened_at).total_seconds())
            self._emit(incident, now, "mitigated", f"drained by {change.actor} ({change.status})", change.id)
        else:
            incident.state = IncidentState.ESCALATED
            self._emit(
                incident, now, "escalated", f"drain {change.status}: {summarize(change.checks)}", change.id
            )

    def _first_block(self, action: Action, ref: InterfaceRef, checks: list[CheckResult]) -> bool:
        """Record a blocked attempt only when the set of failing checks changes."""
        signature = tuple(sorted(check.name for check in checks if not check.passed))
        if self._last_block.get((action, ref)) == signature:
            return False
        self._last_block[(action, ref)] = signature
        return True

    # -- change execution ------------------------------------------------------

    def _new_change(
        self,
        action: Action,
        ref: InterfaceRef,
        actor: str,
        reason: str,
        now: datetime,
        incident_id: str | None,
        checks: list[CheckResult],
    ) -> ChangeRecord:
        self._change_seq += 1
        return ChangeRecord(
            id=f"CHG-{self._change_seq:06d}",
            action=action,
            target=ref,
            status=ChangeStatus.BLOCKED,
            actor=actor,
            reason=reason,
            requested_at=now,
            incident_id=incident_id,
            checks=list(checks),
        )

    def _execute(self, change: ChangeRecord, now: datetime) -> None:
        """Apply, verify, and roll back on a failed post-check."""
        ref, want_drained = change.target, change.action == Action.DRAIN
        try:
            (self.executor.drain if want_drained else self.executor.undrain)(ref, change.id)
        except Exception as exc:  # adapters fail in many ways; record and escalate
            change.status = ChangeStatus.FAILED
            change.checks.append(CheckResult("execute", False, f"{type(exc).__name__}: {exc}"))
        else:
            actual = ref in self.executor.drained()
            if actual == want_drained:
                change.status = self.executor.success_status
                change.checks.append(CheckResult("post_check", True, f"device reports {_drain_word(actual)}"))
            else:
                change.checks.append(
                    CheckResult(
                        "post_check",
                        False,
                        f"expected {_drain_word(want_drained)}, device reports {_drain_word(actual)}",
                    )
                )
                change.status = self._rollback(change)

        if change.actor == AUTOMATION_ACTOR:
            # Applies to failures too, so a broken device is retried after the
            # cooldown rather than on every sample.
            self.state.last_auto_action[ref] = now
        self._sync_drain_state(change, now)
        self._record_change(change)

    def _rollback(self, change: ChangeRecord) -> ChangeStatus:
        inverse = self.executor.undrain if change.action == Action.DRAIN else self.executor.drain
        try:
            inverse(change.target, change.id)
        except Exception as exc:
            change.checks.append(CheckResult("rollback", False, f"{type(exc).__name__}: {exc}"))
            return ChangeStatus.FAILED
        change.checks.append(CheckResult("rollback", True, "restored the pre-change state"))
        return ChangeStatus.ROLLED_BACK

    def _sync_drain_state(self, change: ChangeRecord, now: datetime) -> None:
        ref = change.target
        actual = ref in self.executor.drained()
        if actual and ref not in self.state.drained:
            self.state.drained[ref] = DrainInfo(now, change.actor, change.id, change.incident_id)
            if change.actor == AUTOMATION_ACTOR:
                self.state.drain_history.setdefault(ref, []).append(now)
            self._last_block.pop((Action.UNDRAIN, ref), None)
        elif not actual and ref in self.state.drained:
            del self.state.drained[ref]
            self._last_block.pop((Action.DRAIN, ref), None)

    def _record_change(self, change: ChangeRecord) -> None:
        self.audit.append("change", to_jsonable(change), change.requested_at)
        actor_type = "automation" if change.actor == AUTOMATION_ACTOR else "operator"
        self.metrics.changes.labels(change.action.value, change.status.value, actor_type).inc()
        for check in change.failed_checks:
            self.metrics.safety_check_failures.labels(change.action.value, check.name).inc()
        log.info(
            "change %s %s %s: %s",
            change.id,
            change.action,
            change.target,
            change.status,
            extra={
                "event": "change",
                "change_id": change.id,
                "action": change.action.value,
                "target": str(change.target),
                "status": change.status.value,
                "actor": change.actor,
                "failed_checks": [c.name for c in change.failed_checks],
            },
        )

    def _reconcile(self, now: datetime) -> None:
        """Make recorded drain state agree with what the executor reports."""
        actual = self.executor.drained()
        for ref in sorted(set(self.state.drained) - actual):
            log.warning("reconcile: %s recorded as drained but executor reports in service", ref)
            del self.state.drained[ref]
            self.audit.append("reconcile", {"target": str(ref), "drained": False, "actor": "reconcile"}, now)
        for ref in sorted(actual - set(self.state.drained)):
            log.warning("reconcile: %s drained outside NetPulse; treating as operator-owned", ref)
            self.state.drained[ref] = DrainInfo(now, "unknown", "reconcile")
            self.audit.append("reconcile", {"target": str(ref), "drained": True, "actor": "unknown"}, now)

    # -- events ----------------------------------------------------------------

    def _emit(
        self,
        incident: Incident | None,
        at: datetime,
        kind: str,
        detail: str,
        change_id: str | None = None,
        target: InterfaceRef | None = None,
    ) -> None:
        target = target or (incident.target if incident else None)
        assert target is not None
        if incident is not None:
            incident.record(at, kind, detail)
        event = EngineEvent(at, kind, target, detail, incident.id if incident else None, change_id)
        self._events.append(event)
        self.audit.append("event", to_jsonable(event), at)
        log.info(
            "%s %s: %s",
            kind,
            target,
            detail,
            extra={
                "event": kind,
                "target": str(target),
                "incident_id": event.incident_id,
                "change_id": change_id,
            },
        )

    def _store(self, incident: Incident) -> None:
        self._incidents[incident.id] = incident
        self._open[incident.target] = incident.id
        while len(self._incidents) > self._retention:
            oldest_id, oldest = next(iter(self._incidents.items()))
            if oldest.state != IncidentState.RESOLVED:
                break
            del self._incidents[oldest_id]

    def _open_incident(self, ref: InterfaceRef) -> Incident | None:
        incident_id = self._open.get(ref)
        return self._incidents.get(incident_id) if incident_id else None

    # -- operator API ------------------------------------------------------------

    def open_incidents(self) -> list[Incident]:
        return [self._incidents[i] for i in self._open.values() if i in self._incidents]

    def incidents(self, state: IncidentState | None = None, limit: int = 100) -> list[Incident]:
        found = [i for i in reversed(self._incidents.values()) if state is None or i.state == state]
        return found[:limit]

    def incident(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def set_automation(self, enabled: bool, actor: str, reason: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        self.state.automation_enabled = enabled
        self.state.automation_actor = actor
        self.state.automation_reason = reason
        self.audit.append("automation", {"enabled": enabled, "actor": actor, "reason": reason}, now)
        log.warning(
            "automation %s by %s: %s",
            "enabled" if enabled else "DISABLED",
            actor,
            reason,
            extra={"event": "automation", "enabled": enabled, "actor": actor},
        )

    def add_maintenance(
        self,
        device: str,
        interface: str | None,
        start: datetime,
        end: datetime,
        reason: str,
        actor: str,
    ) -> MaintenanceWindow:
        if end <= start:
            raise ValueError("maintenance end must be after start")
        self._maintenance_seq += 1
        window = MaintenanceWindow(
            f"MW-{self._maintenance_seq:06d}", device, interface, start, end, reason, actor
        )
        self.state.maintenance[window.id] = window
        self.audit.append("maintenance.add", to_jsonable(window), utcnow())
        return window

    def remove_maintenance(self, window_id: str, actor: str) -> bool:
        if self.state.maintenance.pop(window_id, None) is None:
            return False
        self.audit.append("maintenance.remove", {"id": window_id, "actor": actor}, utcnow())
        return True

    def request_change(
        self, action: Action, ref: InterfaceRef, actor: str, reason: str, now: datetime | None = None
    ) -> ChangeRecord:
        """An operator-initiated drain or undrain.

        Operator changes skip the automation guards (kill switch, cooldown,
        fleet concurrency, soak, flap damping) but a drain still has to pass
        the redundancy and capacity checks.
        """
        now = now or utcnow()
        self._events = []
        if actor == AUTOMATION_ACTOR:
            raise ChangeConflict(f"actor name {AUTOMATION_ACTOR!r} is reserved for automation")
        drained = ref in self.state.drained
        if action == Action.DRAIN:
            if drained:
                raise ChangeConflict(f"{ref} is already drained")
            checks = self.safety.drain_checks(ref, now, self.state, self.detector.latest, automated=False)
        else:
            if not drained:
                raise ChangeConflict(f"{ref} is not drained")
            checks = self.safety.undrain_checks(ref, now, self.state, automated=False)

        incident = self._open_incident(ref)
        change = self._new_change(action, ref, actor, reason, now, incident.id if incident else None, checks)
        if not all(check.passed for check in checks):
            self._record_change(change)
            return change
        self._execute(change, now)
        if action == Action.DRAIN and incident is not None:
            self._after_drain(incident, change, now)
        return change

    def interface_status(self) -> list[dict[str, Any]]:
        refs = set(self.detector.interfaces()) | set(self.state.drained)
        rows = []
        for ref in sorted(refs):
            sample = self.detector.latest(ref)
            info = self.state.drained.get(ref)
            group = self.settings.topology.group_for(ref)
            incident = self._open_incident(ref)
            rows.append(
                {
                    "target": str(ref),
                    "link_group": group.name if group else None,
                    "last_sample": to_jsonable(sample) if sample else None,
                    "healthy_since": to_jsonable(self.state.healthy_since.get(ref)),
                    "drained": to_jsonable(info) if info else None,
                    "open_incident": incident.id if incident else None,
                }
            )
        return rows


def _drain_word(drained: bool) -> str:
    return "drained" if drained else "in service"

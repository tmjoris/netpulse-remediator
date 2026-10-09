"""Pre-change safety checks.

Every check is evaluated and reported (not just the first failure) so an
on-call engineer reading a blocked change sees the whole picture. Checks fail
closed: missing topology or stale utilization data blocks a drain rather than
being assumed safe.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import SafetyPolicy
from .models import CheckResult, InterfaceRef, MaintenanceWindow, Sample
from .topology import Topology

AUTOMATION_ACTOR = "netpulse"


@dataclass
class DrainInfo:
    since: datetime
    actor: str
    change_id: str
    incident_id: str | None = None
    hold_reason: str | None = None  # why automation is not undraining it

    @property
    def automated(self) -> bool:
        return self.actor == AUTOMATION_ACTOR


@dataclass
class ControlState:
    """Everything the safety checks need to know about the network and operators."""

    automation_enabled: bool = True
    automation_reason: str = "default configuration"
    automation_actor: str = "config"
    drained: dict[InterfaceRef, DrainInfo] = field(default_factory=dict)
    drain_history: dict[InterfaceRef, list[datetime]] = field(default_factory=dict)
    last_auto_action: dict[InterfaceRef, datetime] = field(default_factory=dict)
    maintenance: dict[str, MaintenanceWindow] = field(default_factory=dict)
    # Not persisted: rebuilt from telemetry after a restart.
    healthy_since: dict[InterfaceRef, datetime] = field(default_factory=dict)

    def in_maintenance(self, ref: InterfaceRef, at: datetime) -> MaintenanceWindow | None:
        return next((w for w in self.maintenance.values() if w.covers(ref, at)), None)

    def recent_drains(self, ref: InterfaceRef, since: datetime) -> int:
        return sum(at >= since for at in self.drain_history.get(ref, []))


LatestSample = Callable[[InterfaceRef], Sample | None]


class SafetyChecker:
    def __init__(self, policy: SafetyPolicy, topology: Topology) -> None:
        self.policy = policy
        self.topology = topology

    def drain_checks(
        self,
        ref: InterfaceRef,
        now: datetime,
        state: ControlState,
        latest: LatestSample,
        *,
        automated: bool = True,
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []
        if automated:
            checks += [
                self._kill_switch(state),
                self._maintenance(ref, now, state),
                self._cooldown(ref, now, state),
                self._fleet_concurrency(state),
            ]
        checks += self._redundancy(ref, now, state, latest)
        return checks

    def undrain_checks(
        self, ref: InterfaceRef, now: datetime, state: ControlState, *, automated: bool = True
    ) -> list[CheckResult]:
        if not automated:
            return [CheckResult("operator_override", True, "manual undrain requested by an operator")]
        return [
            self._kill_switch(state),
            self._maintenance(ref, now, state),
            self._cooldown(ref, now, state),
            self._soak(ref, now, state),
            self._flap_damping(ref, now, state),
        ]

    # -- individual checks -------------------------------------------------

    @staticmethod
    def _kill_switch(state: ControlState) -> CheckResult:
        if state.automation_enabled:
            return CheckResult("kill_switch", True, "automation enabled")
        return CheckResult(
            "kill_switch",
            False,
            f"automation disabled by {state.automation_actor}: {state.automation_reason}",
        )

    @staticmethod
    def _maintenance(ref: InterfaceRef, now: datetime, state: ControlState) -> CheckResult:
        window = state.in_maintenance(ref, now)
        if window is None:
            return CheckResult("maintenance", True, "no active maintenance window")
        return CheckResult(
            "maintenance",
            False,
            f"in maintenance {window.id} until {window.end.isoformat()}: {window.reason}",
        )

    def _cooldown(self, ref: InterfaceRef, now: datetime, state: ControlState) -> CheckResult:
        last = state.last_auto_action.get(ref)
        if last is None or now - last >= self.policy.action_cooldown:
            return CheckResult("cooldown", True, "no recent automated action on this interface")
        remaining = self.policy.action_cooldown - (now - last)
        return CheckResult(
            "cooldown", False, f"last automated action {human(now - last)} ago; {human(remaining)} remaining"
        )

    def _fleet_concurrency(self, state: ControlState) -> CheckResult:
        active = sum(info.automated for info in state.drained.values())
        limit = self.policy.max_concurrent_drains
        detail = f"{active} automated drain(s) active, limit {limit}"
        return CheckResult("fleet_concurrency", active < limit, detail)

    def _soak(self, ref: InterfaceRef, now: datetime, state: ControlState) -> CheckResult:
        since = state.healthy_since.get(ref)
        needed = self.policy.undrain_soak
        healthy_for = now - since if since is not None else timedelta(0)
        detail = f"healthy for {human(healthy_for)}, need {human(needed)}"
        return CheckResult("soak", since is not None and healthy_for >= needed, detail)

    def _flap_damping(self, ref: InterfaceRef, now: datetime, state: ControlState) -> CheckResult:
        count = state.recent_drains(ref, now - self.policy.flap_window)
        limit = self.policy.flap_max_drains
        detail = f"{count} automated drain(s) in the last {human(self.policy.flap_window)}, limit {limit}"
        if count >= limit:
            detail += "; holding drained until an operator undrains it"
        return CheckResult("flap_damping", count < limit, detail)

    def _redundancy(
        self, ref: InterfaceRef, now: datetime, state: ControlState, latest: LatestSample
    ) -> list[CheckResult]:
        group = self.topology.group_for(ref)
        if group is None:
            return [CheckResult("topology", False, f"{ref} is not in any link group; redundancy unknown")]
        checks = [CheckResult("topology", True, f"member of {group.name}")]

        survivors = [m for m in group.members if m.ref != ref and m.ref not in state.drained]
        need = self.policy.min_active_members
        checks.append(
            CheckResult(
                "min_active_members",
                len(survivors) >= need,
                f"{len(survivors)} member(s) would remain active in {group.name}, need {need}",
            )
        )

        # Count every member's last observed load, including members drained so
        # recently that their traffic has not yet shown up on the survivors.
        # Members drained earlier report ~0, so including them is harmless.
        traffic_gbps, stale = 0.0, []
        for member in group.members:
            sample = latest(member.ref)
            fresh = sample is not None and now - sample.timestamp <= self.policy.utilization_max_age
            if sample is not None and fresh:
                traffic_gbps += sample.utilization_pct / 100 * member.capacity_gbps
            elif member.ref == ref or member.ref not in state.drained:
                stale.append(str(member.ref))
        if stale:
            checks.append(
                CheckResult("capacity_headroom", False, f"no fresh utilization for {', '.join(stale)}")
            )
            return checks

        capacity = sum(m.capacity_gbps for m in survivors)
        projected = traffic_gbps / capacity * 100 if capacity else float("inf")
        limit = self.policy.max_post_drain_utilization_pct
        checks.append(
            CheckResult(
                "capacity_headroom",
                projected <= limit,
                f"projected {projected:.0f}% on remaining members "
                f"({traffic_gbps:.0f}/{capacity:.0f} Gbps), limit {limit:.0f}%",
            )
        )
        return checks


def human(delta: timedelta) -> str:
    """Render a duration compactly, e.g. ``1h30m`` or ``45s``."""
    seconds = max(0, int(delta.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours}h" if hours else "", f"{minutes}m" if minutes else "", f"{secs}s" if secs else ""]
    return "".join(parts) or "0s"


def summarize(checks: Iterable[CheckResult]) -> str:
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    return "; ".join(failed) if failed else "all checks passed"

"""Executors apply drain/undrain changes to the network.

The engine only talks to the ``Executor`` protocol. A production adapter would
implement it with NETCONF/gNMI/vendor APIs (for example by raising the IGP
metric or shutting the BGP session on the member before disabling it) and
report the device's real drain state from ``drained()``. This repository
ships no such adapter on purpose: nothing here can change a real device.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .models import Action, ChangeStatus, InterfaceRef


class ExecutionError(RuntimeError):
    pass


class Executor(Protocol):
    name: str
    #: status recorded for a change that executed and passed its post-check
    success_status: ChangeStatus

    def drain(self, ref: InterfaceRef, change_id: str) -> None: ...

    def undrain(self, ref: InterfaceRef, change_id: str) -> None: ...

    def drained(self) -> set[InterfaceRef]:
        """The interfaces the network currently has drained (source of truth)."""
        ...


class DryRunExecutor:
    """Shadow mode: track what *would* be drained without touching any device.

    Keeping the shadow state means safety checks such as fleet concurrency and
    capacity headroom behave exactly as they would in production, so a dry-run
    deployment is a faithful rehearsal.
    """

    name = "dry_run"
    success_status = ChangeStatus.DRY_RUN

    def __init__(self, initially_drained: Iterable[InterfaceRef] = ()) -> None:
        self._drained = set(initially_drained)

    def drain(self, ref: InterfaceRef, change_id: str) -> None:
        self._drained.add(ref)

    def undrain(self, ref: InterfaceRef, change_id: str) -> None:
        self._drained.discard(ref)

    def drained(self) -> set[InterfaceRef]:
        return set(self._drained)


class LabExecutor(DryRunExecutor):
    """Simulated device state with fault injection, used by the scenario lab.

    ``fail_on`` makes the next matching change raise (device unreachable,
    commit rejected). ``ignore_on`` makes it silently not take effect, which
    exercises the post-change verification and rollback path.
    """

    name = "lab"
    success_status = ChangeStatus.APPLIED

    def __init__(self, initially_drained: Iterable[InterfaceRef] = ()) -> None:
        super().__init__(initially_drained)
        self.fail_on: set[tuple[Action, InterfaceRef]] = set()
        self.ignore_on: set[tuple[Action, InterfaceRef]] = set()

    def drain(self, ref: InterfaceRef, change_id: str) -> None:
        if self._inject(Action.DRAIN, ref):
            super().drain(ref, change_id)

    def undrain(self, ref: InterfaceRef, change_id: str) -> None:
        if self._inject(Action.UNDRAIN, ref):
            super().undrain(ref, change_id)

    def _inject(self, action: Action, ref: InterfaceRef) -> bool:
        key = (action, ref)
        if key in self.fail_on:
            self.fail_on.discard(key)
            raise ExecutionError(f"lab: injected failure for {action} on {ref}")
        if key in self.ignore_on:
            self.ignore_on.discard(key)
            return False
        return True


def build_executor(mode: str, initially_drained: Iterable[InterfaceRef] = ()) -> Executor:
    if mode == "dry_run":
        return DryRunExecutor(initially_drained)
    if mode == "lab":
        return LabExecutor(initially_drained)
    raise ValueError(f"unknown executor mode {mode!r}")

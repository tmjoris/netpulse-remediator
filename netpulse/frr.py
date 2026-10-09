"""Executor for FRRouting: drain by OSPF cost-out, verified against the RIB.

Draining an interface sets its OSPF cost to ``drain_cost`` on both ends of
the link. That is the graceful way to take a link out of service: OSPF moves
traffic to the remaining equal-cost paths, but because a costed-out link is
still a valid path, traffic is never black-holed if it turns out to be the
last one. The executor then waits until the routing table on the device no
longer forwards anything out of that interface; if that doesn't happen within
``converge_timeout_s`` it restores the original costs and fails the change.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from .config import FrrOptions
from .devices import DeviceError, Transport
from .executors import ExecutionError
from .models import ChangeStatus, InterfaceRef

log = logging.getLogger("netpulse.frr")


class FrrExecutor:
    name = "frr"
    success_status = ChangeStatus.APPLIED

    def __init__(
        self,
        transport: Transport,
        interfaces: Iterable[InterfaceRef],
        options: FrrOptions | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = 0.5,
    ) -> None:
        self.transport = transport
        self.interfaces = tuple(sorted(set(interfaces)))
        self.options = options or FrrOptions()
        self._sleep = sleep
        self._clock = clock
        self._poll_s = poll_s

    # -- Executor protocol ---------------------------------------------------------

    def drain(self, ref: InterfaceRef, change_id: str) -> None:
        targets = self._link_ends(ref)
        applied: list[InterfaceRef] = []
        try:
            for end in targets:
                self._set_cost(end, self.options.drain_cost, change_id)
                applied.append(end)
            self._wait_until(
                lambda: not self.routes_via(ref),
                f"routes on {ref.device} still use {ref.interface}",
            )
        except (DeviceError, ExecutionError) as exc:
            self._restore(applied, change_id)
            raise ExecutionError(f"drain {ref} failed and was reverted: {exc}") from exc

    def undrain(self, ref: InterfaceRef, change_id: str) -> None:
        try:
            for end in self._link_ends(ref):
                self._set_cost(end, self.options.normal_cost, change_id)
        except DeviceError as exc:
            raise ExecutionError(f"undrain {ref} failed: {exc}") from exc

    def drained(self) -> set[InterfaceRef]:
        try:
            return {ref for ref in self.interfaces if self.cost(ref) >= self.options.drain_cost}
        except DeviceError as exc:
            raise ExecutionError(f"cannot read drain state: {exc}") from exc

    # -- device queries --------------------------------------------------------------

    def cost(self, ref: InterfaceRef) -> int:
        data = self._vtysh_json(ref.device, f"show ip ospf interface {ref.interface} json")
        try:
            return int(data["interfaces"][ref.interface]["cost"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DeviceError(f"{ref}: OSPF is not enabled on this interface") from exc

    def routes_via(self, ref: InterfaceRef) -> list[str]:
        """OSPF prefixes on the device with an active next hop out of ``ref``."""
        table = self._vtysh_json(ref.device, "show ip route ospf json")
        prefixes = []
        for prefix, entries in table.items():
            for entry in entries:
                if not entry.get("selected"):
                    continue
                if any(
                    hop.get("active") and hop.get("interfaceName") == ref.interface
                    for hop in entry.get("nexthops", [])
                ):
                    prefixes.append(prefix)
        return sorted(set(prefixes))

    # -- helpers -----------------------------------------------------------------------

    def _link_ends(self, ref: InterfaceRef) -> list[InterfaceRef]:
        peer = self.options.peers.get(ref)
        return [ref, peer] if peer else [ref]

    def _set_cost(self, ref: InterfaceRef, cost: int, change_id: str) -> None:
        log.info(
            "%s: set OSPF cost %s on %s",
            change_id,
            cost,
            ref,
            extra={"change_id": change_id, "target": str(ref), "ospf_cost": cost},
        )
        self.transport.run(
            ref.device,
            [
                "vtysh",
                "-c",
                "configure terminal",
                "-c",
                f"interface {ref.interface}",
                "-c",
                f"ip ospf cost {cost}",
            ],
        )

    def _restore(self, ends: list[InterfaceRef], change_id: str) -> None:
        for end in ends:
            try:
                self._set_cost(end, self.options.normal_cost, change_id)
            except DeviceError:
                log.exception("%s: could not restore cost on %s; check the device by hand", change_id, end)

    def _wait_until(self, condition: Callable[[], bool], failure: str) -> None:
        deadline = self._clock() + self.options.converge_timeout_s
        while not condition():
            if self._clock() >= deadline:
                raise ExecutionError(f"{failure} after {self.options.converge_timeout_s:g}s")
            self._sleep(self._poll_s)

    def _vtysh_json(self, device: str, command: str) -> dict[str, Any]:
        output = self.transport.run(device, ["vtysh", "-c", command])
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise DeviceError(f"{device}: {command!r} did not return JSON") from exc
        if not isinstance(data, dict):
            raise DeviceError(f"{device}: {command!r} returned {type(data).__name__}")
        return data

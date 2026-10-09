"""A tiny stand-in for FRR's vtysh, good enough to exercise the FRR executor.

Each device has interfaces with OSPF costs. The OSPF route to the peer's
loopback uses every interface whose cost equals the lowest cost (ECMP).
"""

from __future__ import annotations

import json

from netpulse.devices import DeviceError


class FakeFrr:
    def __init__(self, interfaces: dict[str, list[str]], normal_cost: int = 10) -> None:
        self.costs = {(d, i): normal_cost for d, ifaces in interfaces.items() for i in ifaces}
        self.down: set[str] = set()  # unreachable devices
        self.frozen = False  # routes stop following cost changes (convergence failure)
        self._routes: dict[str, list[str]] = {}
        self.commands: list[tuple[str, list[str]]] = []
        self._recompute()

    def _recompute(self) -> None:
        if self.frozen:
            return
        for device in {d for d, _ in self.costs}:
            ifaces = {i: c for (d, i), c in self.costs.items() if d == device}
            best = min(ifaces.values())
            self._routes[device] = sorted(i for i, c in ifaces.items() if c == best)

    def run(self, device: str, argv: list[str]) -> str:
        self.commands.append((device, argv))
        if device in self.down:
            raise DeviceError(f"{device}: unreachable")
        commands = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
        if commands[0] == "configure terminal":
            iface = commands[1].split()[1]
            cost = int(commands[2].split()[-1])
            if (device, iface) not in self.costs:
                raise DeviceError(f"{device}: no interface {iface}")
            self.costs[(device, iface)] = cost
            self._recompute()
            return ""
        if commands[0].startswith("show ip ospf interface"):
            iface = commands[0].split()[4]
            if (device, iface) not in self.costs:
                return json.dumps({})
            return json.dumps({"interfaces": {iface: {"cost": self.costs[(device, iface)]}}})
        if commands[0] == "show ip route ospf json":
            hops = [{"interfaceName": i, "active": True} for i in self._routes[device]]
            return json.dumps({"10.255.0.99/32": [{"selected": True, "nexthops": hops}]})
        raise AssertionError(f"unexpected command {commands}")

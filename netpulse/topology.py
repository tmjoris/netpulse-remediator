"""Redundancy model used by the safety checks.

A link group is a set of parallel interfaces that can carry each other's traffic:
a LAG/bundle, or the members of an ECMP group toward the same neighbour. Draining
one member is only safe when the survivors can absorb its traffic.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import InterfaceRef


@dataclass(frozen=True)
class Member:
    ref: InterfaceRef
    capacity_gbps: float


@dataclass(frozen=True)
class LinkGroup:
    name: str
    members: tuple[Member, ...]

    def member(self, ref: InterfaceRef) -> Member | None:
        return next((m for m in self.members if m.ref == ref), None)


class Topology:
    def __init__(self, groups: Iterable[LinkGroup] = ()) -> None:
        self.groups: tuple[LinkGroup, ...] = tuple(groups)
        self._by_ref: dict[InterfaceRef, LinkGroup] = {}
        names: set[str] = set()
        for group in self.groups:
            if group.name in names:
                raise ValueError(f"duplicate link group name {group.name!r}")
            names.add(group.name)
            if len(group.members) < 2:
                raise ValueError(f"link group {group.name!r} needs at least two members")
            for member in group.members:
                if member.capacity_gbps <= 0:
                    raise ValueError(f"{member.ref} in {group.name!r} must have positive capacity")
                if member.ref in self._by_ref:
                    other = self._by_ref[member.ref].name
                    raise ValueError(f"{member.ref} appears in both {other!r} and {group.name!r}")
                self._by_ref[member.ref] = group

    def group_for(self, ref: InterfaceRef) -> LinkGroup | None:
        return self._by_ref.get(ref)

    def __len__(self) -> int:
        return len(self.groups)

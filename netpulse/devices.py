"""Running commands on network devices.

Two transports: ``docker exec`` for the containerised FRR lab, and ``ssh`` for
real Linux-based routers (FRR on a server, SONiC, Cumulus). Both run a plain
argv on the device and return stdout; anything else is a ``DeviceError``.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol

from .config import DeviceAccess

log = logging.getLogger("netpulse.devices")


class DeviceError(RuntimeError):
    pass


class Transport(Protocol):
    def run(self, device: str, argv: list[str]) -> str: ...


class SubprocessTransport:
    def __init__(self, access: DeviceAccess) -> None:
        self.access = access

    def command(self, device: str, argv: list[str]) -> list[str]:
        host = self.access.hosts.get(device)
        if host is None:
            raise DeviceError(f"no host configured for device {device!r}")
        if self.access.transport == "docker":
            return ["docker", "exec", host, *argv]
        target = f"{self.access.ssh_user}@{host}" if self.access.ssh_user else host
        # BatchMode: never hang on a password prompt in automation.
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "--", *argv]

    def run(self, device: str, argv: list[str]) -> str:
        command = self.command(device, argv)
        log.debug("run on %s: %s", device, " ".join(argv), extra={"device": device})
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, timeout=self.access.command_timeout_s, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeviceError(f"{device}: {type(exc).__name__}: {exc}") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise DeviceError(f"{device}: exit {done.returncode}: {detail[-1] if detail else 'no output'}")
        return done.stdout

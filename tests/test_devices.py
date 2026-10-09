import subprocess

import pytest

from netpulse.config import DeviceAccess
from netpulse.devices import DeviceError, SubprocessTransport


def test_command_construction() -> None:
    docker = SubprocessTransport(DeviceAccess("docker", {"r1": "lab-r1"}))
    assert docker.command("r1", ["vtysh", "-c", "show version"]) == [
        "docker",
        "exec",
        "lab-r1",
        "vtysh",
        "-c",
        "show version",
    ]
    ssh = SubprocessTransport(DeviceAccess("ssh", {"r1": "r1.example.net"}, ssh_user="netops"))
    assert ssh.command("r1", ["vtysh"])[-3:] == ["netops@r1.example.net", "--", "vtysh"]
    assert "BatchMode=yes" in ssh.command("r1", ["vtysh"])
    with pytest.raises(DeviceError, match="no host"):
        docker.command("r9", ["true"])


def test_run_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SubprocessTransport(DeviceAccess("docker", {"r1": "lab-r1"}))

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "Error: No such container: lab-r1\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DeviceError, match="No such container"):
        transport.run("r1", ["true"])

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DeviceError, match="TimeoutExpired"):
        transport.run("r1", ["true"])

    monkeypatch.setattr(
        subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 0, "ok\n", "")
    )
    assert transport.run("r1", ["true"]) == "ok\n"

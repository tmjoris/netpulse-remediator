"""Integration test against the real FRR lab (lab/frr). Opt in with NETPULSE_FRR_LAB=1."""

import os
from pathlib import Path

import pytest

from netpulse.collector import ProbeCollector
from netpulse.config import load_settings
from netpulse.devices import SubprocessTransport
from netpulse.executors import build_executor
from netpulse.frr import FrrExecutor
from netpulse.models import InterfaceRef

pytestmark = pytest.mark.skipif(os.environ.get("NETPULSE_FRR_LAB") != "1", reason="needs the FRR lab running")

CONFIG = Path(__file__).parent.parent / "lab" / "frr" / "netpulse.toml"
L1 = InterfaceRef("r1", "lnk1")


def test_real_cost_out_moves_traffic() -> None:
    settings = load_settings(CONFIG, env={})
    executor = build_executor(settings)
    assert isinstance(executor, FrrExecutor)
    assert executor.drained() == set()
    assert executor.routes_via(L1)
    try:
        executor.drain(L1, "TEST-1")
        assert executor.drained() == {L1}
        assert executor.routes_via(L1) == []
        assert executor.routes_via(InterfaceRef("r2", "lnk1")) == []
    finally:
        executor.undrain(L1, "TEST-2")
    assert executor.drained() == set()


def test_real_probes() -> None:
    settings = load_settings(CONFIG, env={})
    collector = ProbeCollector(
        SubprocessTransport(settings.devices), settings.probes, settings.topology, count=5
    )
    samples = collector.collect()
    assert {s.interface for s in samples} == {"lnk1", "lnk2"}
    assert all(s.packet_loss_pct == 0 for s in samples)

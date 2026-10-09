from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from netpulse.audit import AuditLog
from netpulse.config import Settings
from netpulse.engine import RemediationEngine
from netpulse.executors import LabExecutor
from netpulse.models import InterfaceRef, Sample
from netpulse.simulator import LAB_TOPOLOGY

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LON = [InterfaceRef("edge-dub-01", f"et-0/0/{i}") for i in range(4)]
AMS = [InterfaceRef("edge-dub-02", f"et-0/0/{i}") for i in range(2)]


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def sample(
    ref: InterfaceRef, minute: float, loss: float = 0.0, latency: float = 20.0, util: float = 40.0
) -> Sample:
    return Sample(ref.device, ref.interface, at(minute), latency, loss, util)


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def settings(audit_path: Path) -> Settings:
    return Settings(topology=LAB_TOPOLOGY, executor="lab", audit_path=audit_path)


EngineFactory = Callable[..., RemediationEngine]


@pytest.fixture
def make_engine(settings: Settings) -> EngineFactory:
    def factory(executor: LabExecutor | None = None, **overrides: object) -> RemediationEngine:
        from dataclasses import replace

        config = replace(settings, **overrides) if overrides else settings
        return RemediationEngine(
            config,
            executor=executor or LabExecutor(),
            audit=AuditLog(config.audit_path, fsync=False),
        )

    return factory


def feed(
    engine: RemediationEngine, minute: float, faulty: dict[InterfaceRef, float], util: float = 37.5
) -> list:
    """Send one sample for every lab interface; ``faulty`` maps refs to loss %."""
    results = []
    for ref in LON + AMS:
        drained = ref in engine.state.drained
        results.append(
            engine.ingest(sample(ref, minute, faulty.get(ref, 0.0), util=0.0 if drained else util))
        )
    return results

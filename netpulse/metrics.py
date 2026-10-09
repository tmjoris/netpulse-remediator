"""Prometheus instrumentation.

Counters are incremented by the engine as things happen. State gauges are
computed from the engine at scrape time, so they can never drift from the
state they describe.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

if TYPE_CHECKING:
    from .engine import RemediationEngine


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.samples = Counter(
            "netpulse_telemetry_samples", "Telemetry samples received, by outcome", ["result"], registry=r
        )
        self.incidents_opened = Counter(
            "netpulse_incidents_opened", "Incidents opened, by initial severity", ["severity"], registry=r
        )
        self.incidents_resolved = Counter("netpulse_incidents_resolved", "Incidents resolved", registry=r)
        self.changes = Counter(
            "netpulse_changes",
            "Change requests, by action, final status and actor type",
            ["action", "status", "actor_type"],
            registry=r,
        )
        self.safety_check_failures = Counter(
            "netpulse_safety_check_failures",
            "Failed safety checks on recorded change attempts",
            ["action", "check"],
            registry=r,
        )
        self.evaluation_seconds = Histogram(
            "netpulse_evaluation_duration_seconds",
            "Time to evaluate one telemetry sample, including any remediation",
            buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
            registry=r,
        )
        self.time_to_mitigate = Histogram(
            "netpulse_time_to_mitigate_seconds",
            "Event time from incident open to an effective drain",
            buckets=(30, 60, 120, 300, 600, 1200, 1800, 3600),
            registry=r,
        )

    def bind(self, engine: RemediationEngine) -> None:
        self.registry.register(_StateCollector(lambda: engine))


class _StateCollector(Collector):
    def __init__(self, engine: Callable[[], RemediationEngine]) -> None:
        self._engine = engine

    def collect(self) -> Iterator[Metric]:
        engine = self._engine()
        state = engine.state

        enabled = GaugeMetricFamily("netpulse_automation_enabled", "1 when automated remediation is enabled")
        enabled.add_metric([], 1.0 if state.automation_enabled else 0.0)
        yield enabled

        drained = GaugeMetricFamily(
            "netpulse_drained_interfaces", "Interfaces currently drained, by owner", labels=["owner"]
        )
        automated = sum(info.automated for info in state.drained.values())
        drained.add_metric(["automation"], automated)
        drained.add_metric(["operator"], len(state.drained) - automated)
        yield drained

        held = GaugeMetricFamily(
            "netpulse_drains_held", "Automated drains that automation will not undrain (needs a human)"
        )
        held.add_metric([], sum(info.hold_reason is not None for info in state.drained.values()))
        yield held

        incidents = GaugeMetricFamily(
            "netpulse_open_incidents", "Unresolved incidents", labels=["severity", "state"]
        )
        counts: dict[tuple[str, str], int] = {}
        for incident in engine.open_incidents():
            key = (incident.severity.value, incident.state.value)
            counts[key] = counts.get(key, 0) + 1
        for (severity, incident_state), count in sorted(counts.items()):
            incidents.add_metric([severity, incident_state], count)
        yield incidents

        maintenance = GaugeMetricFamily("netpulse_maintenance_windows", "Configured maintenance windows")
        maintenance.add_metric([], len(state.maintenance))
        yield maintenance

        last_seen = GaugeMetricFamily(
            "netpulse_interface_last_sample_timestamp_seconds",
            "Event timestamp of the most recent sample per interface",
            labels=["device", "interface"],
        )
        for ref in engine.detector.interfaces():
            sample = engine.detector.latest(ref)
            if sample is not None:
                last_seen.add_metric([ref.device, ref.interface], sample.timestamp.timestamp())
        yield last_seen

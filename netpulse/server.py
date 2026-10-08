from datetime import datetime, timezone
from threading import Lock

from fastapi import FastAPI
from pydantic import BaseModel, Field
from prometheus_client import Counter, Gauge, generate_latest
from starlette.responses import Response

from .engine import RemediationEngine
from .executor import DryRunExecutor
from .models import Incident, Sample

app = FastAPI(title="NetPulse Remediator", version="0.2.0")
engine = RemediationEngine()
executor = DryRunExecutor()
lock = Lock()
incidents: list[Incident] = []
telemetry_samples = Counter("netpulse_telemetry_samples_total", "Telemetry samples received")
active_incidents = Gauge("netpulse_active_incidents", "Distinct interfaces with an active incident")
remediation_actions = Counter("netpulse_remediation_actions_total", "Remediation plans emitted", ["action"])


class Telemetry(BaseModel):
    device: str = Field(min_length=1)
    interface: str = Field(min_length=1)
    timestamp: datetime | None = None
    latency_ms: float = Field(ge=0)
    packet_loss_pct: float = Field(ge=0, le=100)
    utilization_pct: float = Field(ge=0, le=100)


@app.post("/telemetry")
def receive_telemetry(payload: Telemetry) -> dict[str, object]:
    sample = Sample(
        device=payload.device,
        interface=payload.interface,
        timestamp=payload.timestamp or datetime.now(timezone.utc),
        latency_ms=payload.latency_ms,
        packet_loss_pct=payload.packet_loss_pct,
        utilization_pct=payload.utilization_pct,
    )
    with lock:
        telemetry_samples.inc()
        incident = engine.evaluate(sample)
        if incident:
            incidents.append(incident)
            remediation_actions.labels(incident.action.value).inc()
            plan = executor.execute(incident)
            active_incidents.set(len({item.key for item in incidents}))
            return {"status": "incident", "incident": incident, "plan": plan}
    return {"status": "accepted"}


@app.get("/incidents")
def get_incidents() -> list[Incident]:
    with lock:
        return incidents[-50:]


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}

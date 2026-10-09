"""HTTP API: telemetry ingestion, incident and drain inspection, operator controls."""

from __future__ import annotations

import hmac
import logging
import threading
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from . import __version__
from .config import Settings, load_settings
from .engine import ChangeConflict, RemediationEngine
from .models import Action, ChangeStatus, IncidentState, InterfaceRef, Sample, to_jsonable, utcnow

log = logging.getLogger("netpulse.server")

MAX_BATCH = 5000
MAX_MAINTENANCE = timedelta(days=7)
# Module level so FastAPI can resolve it from the (postponed) annotations below.
_bearer = HTTPBearer(auto_error=False)


class TelemetryIn(BaseModel):
    device: str = Field(min_length=1, max_length=128)
    interface: str = Field(min_length=1, max_length=128)
    timestamp: AwareDatetime | None = None
    latency_ms: float = Field(ge=0)
    packet_loss_pct: float = Field(ge=0, le=100)
    utilization_pct: float = Field(ge=0, le=100)


class TelemetryBatch(BaseModel):
    samples: list[TelemetryIn] = Field(min_length=1, max_length=MAX_BATCH)


class Operator(BaseModel):
    actor: str = Field(min_length=1, max_length=64, description="who is making the change, e.g. an LDAP name")
    reason: str = Field(min_length=3, max_length=500)


class AutomationIn(Operator):
    enabled: bool


class MaintenanceIn(Operator):
    device: str = Field(min_length=1)
    interface: str | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    duration_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_end(self) -> MaintenanceIn:
        if (self.end is None) == (self.duration_minutes is None):
            raise ValueError("provide exactly one of end or duration_minutes")
        return self


class ChangeIn(Operator):
    action: Action
    device: str = Field(min_length=1)
    interface: str = Field(min_length=1)


def create_app(settings: Settings | None = None, *, engine: RemediationEngine | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or RemediationEngine(settings)
    lock = threading.Lock()
    app = FastAPI(
        title="NetPulse Remediator",
        version=__version__,
        description="Telemetry-driven detection and guarded drain/undrain remediation.",
    )
    app.state.engine = engine

    if not settings.api_token:
        log.warning("NETPULSE_API_TOKEN is not set: write endpoints are unauthenticated")
    if not len(settings.topology):
        log.warning("no link groups configured: every automated drain will fail the topology check")

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> None:
        if not settings.api_token:
            return
        if credentials is None or not hmac.compare_digest(credentials.credentials, settings.api_token):
            raise HTTPException(
                401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"}
            )

    write = [Depends(authorize)]

    @app.get("/healthz", tags=["probes"])
    def healthz() -> dict[str, str]:
        """Liveness: the process is serving requests."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["probes"])
    def readyz() -> JSONResponse:
        """Readiness: the audit log can be written, so changes can be recorded."""
        ready = engine.audit.writable()
        body = {"status": "ready" if ready else "not_ready", "audit_writable": ready}
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/metrics", tags=["probes"])
    def metrics() -> Response:
        with lock:
            payload = generate_latest(engine.metrics.registry)
        return Response(payload, media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/status", tags=["operations"])
    def status() -> dict[str, Any]:
        with lock:
            return {
                "version": __version__,
                "executor": engine.executor.name,
                "config": settings.source,
                "link_groups": len(settings.topology),
                "automation": {
                    "enabled": engine.state.automation_enabled,
                    "actor": engine.state.automation_actor,
                    "reason": engine.state.automation_reason,
                },
                "drained": len(engine.state.drained),
                "open_incidents": len(engine.open_incidents()),
            }

    @app.post("/v1/telemetry", tags=["telemetry"], dependencies=write)
    def telemetry(batch: TelemetryBatch) -> dict[str, Any]:
        received_at = utcnow()
        accepted, rejected = 0, []
        events: list[Any] = []
        changes: list[Any] = []
        with lock:
            for item in batch.samples:
                sample = Sample(
                    item.device,
                    item.interface,
                    item.timestamp or received_at,
                    item.latency_ms,
                    item.packet_loss_pct,
                    item.utilization_pct,
                )
                result = engine.ingest(sample)
                if result.accepted:
                    accepted += 1
                else:
                    rejected.append({"target": str(result.target), "error": result.error})
                events += result.events
                changes += result.changes
            return {
                "accepted": accepted,
                "rejected": rejected,
                "events": to_jsonable(events),
                "changes": to_jsonable(changes),
            }

    @app.get("/v1/interfaces", tags=["operations"])
    def interfaces() -> list[dict[str, Any]]:
        with lock:
            return engine.interface_status()

    @app.get("/v1/incidents", tags=["incidents"])
    def incidents(
        state: IncidentState | None = None,
        active: Annotated[bool, Query(description="only unresolved incidents")] = False,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[Any]:
        with lock:
            found = engine.open_incidents() if active else engine.incidents(state, limit)
            if active and state is not None:
                found = [i for i in found if i.state == state]
            return to_jsonable(found[:limit])  # type: ignore[no-any-return]

    @app.get("/v1/incidents/{incident_id}", tags=["incidents"])
    def incident(incident_id: str) -> Any:
        with lock:
            found = engine.incident(incident_id)
            if found is None:
                raise HTTPException(404, f"incident {incident_id} not found (it may have been evicted)")
            return to_jsonable(found)

    @app.get("/v1/drains", tags=["remediation"])
    def drains() -> list[dict[str, Any]]:
        with lock:
            return [
                {"target": str(ref), **to_jsonable(info)}
                for ref, info in sorted(engine.state.drained.items())
            ]

    @app.post("/v1/changes", tags=["remediation"], dependencies=write)
    def change(body: ChangeIn) -> JSONResponse:
        """Operator drain/undrain. Drains still pass redundancy and capacity checks."""
        with lock:
            try:
                record = engine.request_change(
                    body.action, InterfaceRef(body.device, body.interface), body.actor, body.reason
                )
            except ChangeConflict as exc:
                raise HTTPException(409, str(exc)) from exc
        code = {ChangeStatus.BLOCKED: 409, ChangeStatus.FAILED: 502, ChangeStatus.ROLLED_BACK: 502}
        return JSONResponse(to_jsonable(record), status_code=code.get(record.status, 200))

    @app.get("/v1/automation", tags=["operations"])
    def get_automation() -> dict[str, Any]:
        with lock:
            return {
                "enabled": engine.state.automation_enabled,
                "actor": engine.state.automation_actor,
                "reason": engine.state.automation_reason,
            }

    @app.put("/v1/automation", tags=["operations"], dependencies=write)
    def put_automation(body: AutomationIn) -> dict[str, Any]:
        """Kill switch. Persisted in the audit log, so it survives restarts."""
        with lock:
            engine.set_automation(body.enabled, body.actor, body.reason)
        return get_automation()

    @app.get("/v1/maintenance", tags=["operations"])
    def list_maintenance() -> list[Any]:
        with lock:
            return to_jsonable(sorted(engine.state.maintenance.values(), key=lambda w: w.start))  # type: ignore[no-any-return]

    @app.post("/v1/maintenance", tags=["operations"], dependencies=write, status_code=201)
    def add_maintenance(body: MaintenanceIn) -> Any:
        start = body.start or utcnow()
        end = body.end or start + timedelta(minutes=body.duration_minutes or 0)
        if end <= start:
            raise HTTPException(422, "maintenance end must be after start")
        if end - start > MAX_MAINTENANCE:
            raise HTTPException(422, f"maintenance windows are limited to {MAX_MAINTENANCE.days} days")
        with lock:
            window = engine.add_maintenance(body.device, body.interface, start, end, body.reason, body.actor)
        return to_jsonable(window)

    @app.delete("/v1/maintenance/{window_id}", tags=["operations"], dependencies=write, status_code=204)
    def delete_maintenance(window_id: str, actor: Annotated[str, Query(min_length=1)]) -> Response:
        with lock:
            if not engine.remove_maintenance(window_id, actor):
                raise HTTPException(404, f"maintenance window {window_id} not found")
        return Response(status_code=204)

    return app

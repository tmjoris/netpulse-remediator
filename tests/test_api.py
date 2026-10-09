from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from netpulse.audit import AuditLog
from netpulse.config import Settings
from netpulse.engine import RemediationEngine
from netpulse.executors import LabExecutor
from netpulse.server import create_app

from .conftest import AMS, LON, T0

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(settings: Settings) -> TestClient:
    settings = Settings(
        topology=settings.topology, executor="lab", audit_path=settings.audit_path, api_token=TOKEN
    )
    engine = RemediationEngine(
        settings, executor=LabExecutor(), audit=AuditLog(settings.audit_path, fsync=False)
    )
    return TestClient(create_app(settings, engine=engine))


def batch(minute: int, loss: dict | None = None, util: float = 37.5) -> dict:
    loss = loss or {}
    ts = (T0 + timedelta(minutes=minute)).isoformat()
    return {
        "samples": [
            {
                "device": ref.device,
                "interface": ref.interface,
                "timestamp": ts,
                "latency_ms": 20,
                "packet_loss_pct": loss.get(ref, 0.0),
                "utilization_pct": util,
            }
            for ref in LON + AMS
        ]
    }


def test_probes(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    status = client.get("/v1/status").json()
    assert status["executor"] == "lab" and status["link_groups"] == 2


def test_writes_require_token(client: TestClient) -> None:
    assert client.post("/v1/telemetry", json=batch(0)).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/telemetry", json=batch(0), headers=bad).status_code == 401
    assert client.post("/v1/telemetry", json=batch(0), headers=AUTH).status_code == 200


def test_telemetry_drives_detection_and_drain(client: TestClient) -> None:
    replies = [
        client.post("/v1/telemetry", json=batch(m, {LON[1]: 15}), headers=AUTH).json() for m in range(5)
    ]
    last = replies[-1]
    assert last["accepted"] == 6
    assert [c["status"] for c in last["changes"]] == ["applied"]
    assert {e["kind"] for e in last["events"]} == {"opened", "mitigated"}

    drains = client.get("/v1/drains").json()
    assert drains[0]["target"] == str(LON[1]) and drains[0]["actor"] == "netpulse"
    (incident,) = client.get("/v1/incidents", params={"active": True}).json()
    assert incident["state"] == "mitigated"
    assert client.get(f"/v1/incidents/{incident['id']}").json()["id"] == incident["id"]
    assert client.get("/v1/incidents/INC-999999").status_code == 404
    assert client.get("/v1/incidents", params={"state": "resolved"}).json() == []
    interfaces = {row["target"]: row for row in client.get("/v1/interfaces").json()}
    assert interfaces[str(LON[1])]["open_incident"] == incident["id"]


def test_out_of_order_samples_are_reported(client: TestClient) -> None:
    client.post("/v1/telemetry", json=batch(5), headers=AUTH)
    reply = client.post("/v1/telemetry", json=batch(4), headers=AUTH).json()
    assert reply["accepted"] == 0 and len(reply["rejected"]) == 6


def test_validation_errors(client: TestClient) -> None:
    sample = batch(0)["samples"][0]
    for bad in ({**sample, "packet_loss_pct": 101}, {**sample, "timestamp": "2026-01-01T00:00:00"}):
        assert client.post("/v1/telemetry", json={"samples": [bad]}, headers=AUTH).status_code == 422
    assert client.post("/v1/telemetry", json={"samples": []}, headers=AUTH).status_code == 422


def test_kill_switch(client: TestClient) -> None:
    body = {"enabled": False, "actor": "alice", "reason": "suspect telemetry"}
    assert client.put("/v1/automation", json=body, headers=AUTH).json()["enabled"] is False
    assert client.get("/v1/automation").json()["actor"] == "alice"
    replies = [
        client.post("/v1/telemetry", json=batch(m, {LON[1]: 15}), headers=AUTH).json() for m in range(5)
    ]
    assert replies[-1]["changes"][0]["status"] == "blocked"


def test_maintenance_lifecycle(client: TestClient) -> None:
    body = {"device": "edge-dub-01", "duration_minutes": 60, "actor": "bob", "reason": "linecard swap"}
    created = client.post("/v1/maintenance", json=body, headers=AUTH)
    assert created.status_code == 201
    window_id = created.json()["id"]
    assert [w["id"] for w in client.get("/v1/maintenance").json()] == [window_id]
    assert (
        client.delete(f"/v1/maintenance/{window_id}", params={"actor": "bob"}, headers=AUTH).status_code
        == 204
    )
    assert (
        client.delete(f"/v1/maintenance/{window_id}", params={"actor": "bob"}, headers=AUTH).status_code
        == 404
    )

    too_long = {**body, "duration_minutes": 60 * 24 * 30}
    assert client.post("/v1/maintenance", json=too_long, headers=AUTH).status_code == 422
    both = {**body, "end": "2030-01-01T00:00:00+00:00"}
    assert client.post("/v1/maintenance", json=both, headers=AUTH).status_code == 422
    backwards = {
        **body,
        "duration_minutes": None,
        "start": "2030-01-02T00:00:00+00:00",
        "end": "2030-01-01T00:00:00+00:00",
    }
    assert client.post("/v1/maintenance", json=backwards, headers=AUTH).status_code == 422


def test_operator_changes(client: TestClient) -> None:
    # Telemetry timestamped now, so the capacity check sees fresh utilization.
    for _ in range(2):
        samples = batch(0)["samples"]
        for item in samples:
            item.pop("timestamp")
        client.post("/v1/telemetry", json={"samples": samples}, headers=AUTH)
    drain = {
        "action": "drain",
        "device": LON[0].device,
        "interface": LON[0].interface,
        "actor": "alice",
        "reason": "fibre work",
    }
    assert client.post("/v1/changes", json=drain, headers=AUTH).json()["status"] == "applied"
    assert client.post("/v1/changes", json=drain, headers=AUTH).status_code == 409
    undrain = {**drain, "action": "undrain", "reason": "done"}
    assert client.post("/v1/changes", json=undrain, headers=AUTH).status_code == 200
    blocked = {**drain, "device": "core-01", "interface": "ae0"}
    client.post("/v1/changes", json={**blocked, "action": "undrain"}, headers=AUTH)
    assert client.post("/v1/changes", json=blocked, headers=AUTH).status_code == 409


def test_metrics_exposition(client: TestClient) -> None:
    for minute in range(5):
        client.post("/v1/telemetry", json=batch(minute, {LON[1]: 15}), headers=AUTH)
    text = client.get("/metrics").text
    for name in [
        'netpulse_telemetry_samples_total{result="accepted"} 30.0',
        'netpulse_changes_total{action="drain",actor_type="automation",status="applied"} 1.0',
        'netpulse_drained_interfaces{owner="automation"} 1.0',
        "netpulse_automation_enabled 1.0",
        'netpulse_open_incidents{severity="critical",state="mitigated"} 1.0',
        "netpulse_time_to_mitigate_seconds_count 1.0",
        "netpulse_interface_last_sample_timestamp_seconds",
        "netpulse_evaluation_duration_seconds_bucket",
    ]:
        assert name in text, name


def test_unauthenticated_mode_when_no_token(settings: Settings) -> None:
    engine = RemediationEngine(
        settings, executor=LabExecutor(), audit=AuditLog(settings.audit_path, fsync=False)
    )
    client = TestClient(create_app(settings, engine=engine))
    assert client.post("/v1/telemetry", json=batch(0)).status_code == 200

# NetPulse Remediator

[![CI](https://github.com/tmjoris/netpulse-remediator/actions/workflows/ci.yml/badge.svg)](https://github.com/tmjoris/netpulse-remediator/actions/workflows/ci.yml)

Telemetry-driven detection of degraded network links, with **guarded,
audited drain/undrain remediation**.

```text
interface telemetry ──> M-of-N detector ──> incident lifecycle ──> safety checks ──> executor
                         (hysteresis)        open/mitigated/          capacity,         drain/undrain
                                             escalated/resolved       blast radius,     + post-check
                                                                      kill switch, ...  + rollback
                                    every decision ──> hash-chained audit log ──> replayed on restart
```

## Why this exists

Routing protocols handle *hard* failures well: a link goes down, BFD/IS-IS/BGP
notice, and traffic moves. They handle *gray* failures badly: an optic that
drops 15% of packets still looks "up", so traffic keeps flowing into it. At
scale, the standard answer is automation that **drains** the bad link (moves
traffic off it before taking it out of service) and undrains it once it's
healthy again.

But remediation automation is also one of the most dangerous things you can
run against a network. A bug or bad telemetry feed that drains links in a loop
can turn one sick optic into an outage. Most of this project is about that
second problem: **making automation refuse to act when acting is unsafe**,
and making every decision explainable after the fact.

## What it does

| Concern | Behaviour |
|---|---|
| **Detection** | Per-interface sliding window. *M-of-N* rule (default 4 of 5 samples) so one noisy sample never pages anyone. Separate, lower *clear* thresholds (hysteresis) so a link hovering at the threshold doesn't flap the incident. Out-of-order and duplicate samples are rejected. |
| **Incident lifecycle** | `open → mitigated / escalated → resolved`, deduplicated per interface, with a timeline of events for the postmortem. Warning incidents alert only; **critical** incidents trigger a drain. |
| **Safety checks** | Every drain is checked for: kill switch, maintenance window, per-interface cooldown, fleet-wide concurrent-drain limit, link-group membership, minimum surviving members, and **projected utilization on the survivors**. Checks fail closed: unknown topology or stale utilization blocks the drain. |
| **Undrain** | Only after the link has been continuously healthy for a soak period (15m). **Flap damping**: after 3 automated drains in 24h, the link is held drained for a human. Drains made by operators are never undone by automation. |
| **Change safety** | Each change is applied, then **verified** against the device's reported state. If the post-check fails it is rolled back. Failures escalate and are retried only after the cooldown. |
| **Audit & state** | Append-only JSONL with a SHA-256 hash chain (tampering is detectable with `netpulse audit verify`). On restart the log is replayed so the kill switch, maintenance windows, drain ownership and flap history survive, then reconciled against what the executor reports. |
| **Operations** | Versioned HTTP API with bearer-token auth on writes, liveness/readiness probes, Prometheus metrics, 8 alert rules each linked to a [runbook](docs/RUNBOOK.md) section, and a provisioned Grafana dashboard. |

There is **no live device adapter** in this repository, by design. The
`dry_run` executor runs in shadow mode: it records what it *would* do and
tracks the would-be state, so every safety check behaves exactly as it would in
production. See [Architecture → Executors](docs/ARCHITECTURE.md#executors) for
what a real adapter would need.

## Quick start

Requires Python 3.11+.

```bash
make install                      # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/netpulse simulate --list
.venv/bin/netpulse simulate --scenario all
```

### Failure scenarios

The lab models two link groups (a 4×100G bundle at 150 Gbps and a 2×100G ECMP
pair at 120 Gbps) and redistributes traffic when a member is drained, so drains
have visible consequences. Each scenario's outcome is pinned by a test.

| Scenario | What happens | What NetPulse should do |
|---|---|---|
| `gray-failure` | One bundle member silently drops ~15% of packets | Drain it (survivors go 38% → 50%), soak 15 min after recovery, undrain |
| `insufficient-capacity` | A member of a 2-link group at 60% load starts dropping | **Refuse** to drain (survivor would hit ~120%) and escalate to a human |
| `correlated-failure` | A line-card fault hits 3 of 4 bundle members | Drain two, **block the third** (fleet limit + capacity), undrain after recovery |
| `flapping` | A member degrades for 5 minutes every 35 minutes | Drain/undrain twice, then **hold it drained** for a human (flap damping) |
| `change-verification` | The device accepts the drain but it never takes effect | Post-check fails, **roll back**, retry after the cooldown |

Example (`netpulse simulate --scenario insufficient-capacity`):

```text
T+  4m30s  change     CHG-000001 edge-dub-02:et-0/0/0   drain -> blocked
                                                           x capacity_headroom: projected 119% on remaining members (119/100 Gbps), limit 80%
T+  4m30s  opened     INC-000001 edge-dub-02:et-0/0/0   critical: 4/5 samples breaching (4 critical, need 4); mean loss 9.73%, mean latency 42.1ms
T+  4m30s  escalated  INC-000001 edge-dub-02:et-0/0/0   drain blocked: capacity_headroom: projected 119% on remaining members (119/100 Gbps), limit 80%
T+ 31m30s  resolved   INC-000001 edge-dub-02:et-0/0/0   healthy: 4/5 samples below clear thresholds
```

### Run the service

```bash
.venv/bin/netpulse check-config config/netpulse.example.toml
NETPULSE_API_TOKEN=change-me .venv/bin/netpulse serve --config config/netpulse.example.toml
```

### Run the full stack (NetPulse + Prometheus + Grafana)

```bash
make up                                                        # docker compose up --build -d --wait
.venv/bin/netpulse simulate --scenario all --target http://127.0.0.1:8000
```

- API docs (OpenAPI): <http://127.0.0.1:8000/docs>
- Prometheus (with alert rules): <http://127.0.0.1:9090/alerts>
- Grafana dashboard: <http://127.0.0.1:3000> (anonymous read-only)

The container runs as a non-root user with a read-only root filesystem, no
Linux capabilities, a health check on `/readyz`, and the audit log on a named
volume. All ports bind to `127.0.0.1`.

## API

Write endpoints require `Authorization: Bearer $NETPULSE_API_TOKEN` when a token is configured.

| Method & path | Purpose |
|---|---|
| `GET /healthz`, `GET /readyz` | Liveness; readiness (audit log writable) |
| `GET /metrics` | Prometheus exposition |
| `GET /v1/status` | Version, executor mode, automation state, counts |
| `POST /v1/telemetry` | Batch of up to 5000 samples; returns events and changes they caused |
| `GET /v1/interfaces` | Per-interface last sample, health, drain and incident |
| `GET /v1/incidents?active=true&state=…` | Incidents (most recent first); `GET /v1/incidents/{id}` for the timeline |
| `GET /v1/drains` | Current drains with owner and hold reason |
| `POST /v1/changes` | Operator drain/undrain (`409` if blocked, `502` if the device change failed) |
| `GET`/`PUT /v1/automation` | Kill switch (persisted across restarts) |
| `GET`/`POST /v1/maintenance`, `DELETE /v1/maintenance/{id}?actor=` | Maintenance windows (max 7 days) |

## Metrics and alerts

| Metric | Type |
|---|---|
| `netpulse_telemetry_samples_total{result}` | counter |
| `netpulse_incidents_opened_total{severity}`, `netpulse_incidents_resolved_total` | counter |
| `netpulse_changes_total{action,status,actor_type}` | counter |
| `netpulse_safety_check_failures_total{action,check}` | counter |
| `netpulse_time_to_mitigate_seconds` | histogram (incident open → effective drain) |
| `netpulse_evaluation_duration_seconds` | histogram |
| `netpulse_automation_enabled`, `netpulse_drains_held`, `netpulse_maintenance_windows` | gauge |
| `netpulse_drained_interfaces{owner}`, `netpulse_open_incidents{severity,state}` | gauge |
| `netpulse_interface_last_sample_timestamp_seconds{device,interface}` | gauge |

State gauges are computed at scrape time from the engine, so they cannot drift.
Alert rules live in [`prometheus/alerts.yml`](prometheus/alerts.yml); every
alert has a matching section in the [runbook](docs/RUNBOOK.md).

## Configuration

One TOML file ([`config/netpulse.example.toml`](config/netpulse.example.toml))
holds detection thresholds, safety policy and the link-group topology.
Unknown keys, wrong types and inconsistent thresholds are rejected at startup.
`NETPULSE_CONFIG`, `NETPULSE_API_TOKEN`, `NETPULSE_AUDIT_PATH` and
`NETPULSE_EXECUTOR` override the file; secrets belong in the environment.

## Repository map

| Path | Responsibility |
|---|---|
| `netpulse/detector.py` | Sliding window, M-of-N, hysteresis, out-of-order rejection |
| `netpulse/engine.py` | Incident lifecycle, drain/undrain orchestration, post-check, rollback, replay, reconcile |
| `netpulse/safety.py` | Safety checks and control state |
| `netpulse/topology.py` | Link groups (redundancy model) |
| `netpulse/executors.py` | `Executor` protocol, dry-run and lab executors |
| `netpulse/audit.py` | Hash-chained audit log and verification |
| `netpulse/server.py` | FastAPI app |
| `netpulse/metrics.py` | Prometheus instrumentation |
| `netpulse/simulator.py` | Failure-scenario lab (in-process or over HTTP) |
| `netpulse/config.py` | TOML + env config with validation |
| `prometheus/`, `grafana/` | Scrape config, alert rules, dashboard provisioning |
| `docs/ARCHITECTURE.md` | Design, state machine, safety model, trade-offs |
| `docs/RUNBOOK.md` | On-call procedures per alert |

## Verification status

Verified locally:

- `ruff`, `ruff format`, `mypy --strict`, and 76 tests with an 85% coverage gate (currently ~92%) on Python 3.12
- all five scenarios in-process, with outcomes asserted by tests
- Docker image build; Compose stack startup with health checks
- all scenarios driven over HTTP against the container; audit chain verified inside it
- kill switch and held drains surviving a container restart
- `promtool` validation of the Prometheus config and all 8 alert rules; every dashboard query evaluated against live Prometheus

Not verified:

- the GitHub Actions workflow itself (it mirrors the local steps but has not run on GitHub yet), including the Python 3.11 and 3.13 matrix legs
- any real network device or vendor API (there is no live adapter)
- sustained high telemetry volume (single process, one lock; see the architecture doc)
- high availability: one instance only, state on a local volume

## Production gaps

What this would need before touching a real network, roughly in order:

1. A device adapter (gNMI/NETCONF or a vendor API) that drains by raising the IGP metric or shutting the BGP session first, and reports real drain state to `drained()`.
2. Topology from the network's source of truth (e.g. NetBox), including shared-risk link groups, rather than a static file.
3. Durable shared state and leader election so two instances can't both act.
4. Real identity: mTLS/OIDC with role-based permissions instead of a single bearer token.
5. Streaming telemetry ingestion (gNMI subscriptions, sFlow, active probes) instead of HTTP push.
6. Cross-signal correlation, so a whole device or site failing is treated as one incident rather than N interface incidents.

# NetPulse Remediator

## What problem this solves

Network operations teams receive a continuous stream of interface measurements.
The difficult part is not reading one bad measurement; it is deciding whether
the service is experiencing a sustained problem and then choosing a safe,
repeatable response.

NetPulse is a small, runnable example of that workflow:

```text
telemetry -> sliding-window evaluation -> incident -> dry-run action -> audit log
```

It is intentionally a learning and portfolio project. It does **not** connect
to a router, change a production route, or claim to be a complete incident
management system.

## What is implemented

### Detection

Each sample contains:

- device name;
- interface name;
- timestamp;
- latency in milliseconds;
- packet loss percentage;
- utilization percentage.

The `RemediationEngine` keeps the most recent five samples for each
`device:interface` by default. It calculates the average latency and packet
loss over that window:

- no incident when average loss is below 2% and average latency is below
  100 ms;
- warning when either threshold is exceeded;
- critical when average packet loss reaches 10%.

The thresholds and window size are constructor parameters, so the policy is
visible and testable rather than hidden in a callback.

### Remediation decision

The engine produces one of these plans:

| Condition | Plan |
|---|---|
| sustained warning | `shift_traffic` |
| sustained critical degradation | `disable_interface` |
| another plan during the ten-minute cooldown | `noop` |

The current executor is deliberately a `DryRunExecutor`. It writes an
append-only JSON Lines audit record and returns what an adapter **would** do.
There is no vendor API, SSH client, or privileged network operation in this
repository.

### HTTP and metrics surface

`netpulse serve` starts a FastAPI application:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | process health check |
| `POST /telemetry` | accept one validated telemetry sample |
| `GET /incidents` | return the most recent in-memory incidents |
| `GET /metrics` | expose Prometheus metrics |

Prometheus metrics include the number of received samples, active incident
interfaces, and remediation plans by action.

## Run it without Docker

Requirements: Python 3.11 or newer and network access to install the
dependencies.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
netpulse demo --samples 8
```

The demo uses generated sample data and prints the resulting plans. It does
not send HTTP requests.

To run the HTTP service:

```bash
netpulse serve
```

In another terminal:

```bash
python examples/send_degraded_telemetry.py
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/incidents
curl http://127.0.0.1:8000/metrics
```

The example sender posts six degraded samples. The first five fill the
sliding window; subsequent requests produce incidents. The response includes
the incident and the dry-run plan.

## Run the local Prometheus stack

Docker and Docker Compose are required for this section:

```bash
docker compose up --build
```

This starts:

```text
NetPulse API: http://127.0.0.1:8000
Prometheus:   http://127.0.0.1:9090
```

Then send telemetry and open the Prometheus UI:

```bash
python examples/send_degraded_telemetry.py
curl http://127.0.0.1:8000/metrics
```

The Compose file only provides a local application and metrics stack. It does
not create a router, emulate packet loss, or make changes to the host network.

## Repository map

| Path | Responsibility |
|---|---|
| `netpulse/models.py` | telemetry, incident, severity, and action types |
| `netpulse/engine.py` | sliding-window policy and cooldown logic |
| `netpulse/executor.py` | dry-run plan execution and audit logging |
| `netpulse/server.py` | HTTP ingestion, incidents, health, and metrics |
| `netpulse/cli.py` | `demo` and `serve` commands |
| `examples/send_degraded_telemetry.py` | real HTTP client example |
| `Dockerfile` | container image for the API |
| `docker-compose.yml` | API plus Prometheus |

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the request lifecycle,
state model, safety boundary, and extension points.

## What has and has not been verified

Verified in the development environment:

- the Python modules compile;
- the API health endpoint responds;
- telemetry can be posted through HTTP;
- an incident and dry-run plan are returned;
- Prometheus metric text is generated;
- the standalone demo runs.

Not verified in the development environment:

- Docker image build;
- Docker Compose startup;
- Prometheus scraping across containers;
- performance under high telemetry volume;
- integration with a real device or vendor API.

Those limits are intentional and documented. A successful local API smoke test
is not the same as proving production readiness.

# NetPulse Remediator

NetPulse is a small, production-shaped network reliability lab. It ingests
time-series interface telemetry, detects sustained packet loss and latency
regressions, and produces auditable remediation plans. The default execution
mode is dry-run, so an engineer can inspect the proposed action before a
change is applied.

The project demonstrates:

- sliding-window SLO evaluation instead of reacting to one noisy sample;
- incident deduplication and severity selection;
- idempotent remediation with cooldowns;
- Prometheus-compatible metrics and JSON incident output;
- explicit dry-run/live boundaries and an append-only audit log.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
netpulse demo --samples 12
netpulse serve
# in another terminal:
python examples/send_degraded_telemetry.py
pytest
```

For a complete local telemetry stack, use Docker Compose:

```bash
docker compose up --build
python examples/send_degraded_telemetry.py
curl http://localhost:8000/incidents
curl http://localhost:8000/metrics
open http://localhost:9090
```

Prometheus scrapes NetPulse every five seconds, so the incident counter and
remediation action counters are visible as real time-series metrics.

The demo simulates a degraded uplink. For live-style telemetry, `netpulse
serve` exposes an HTTP ingestion API, Prometheus `/metrics`, `/incidents`, and
`/healthz`. The example sender posts real HTTP telemetry and receives the
dry-run remediation plan. No router change is made automatically.

## Design notes

NetPulse intentionally separates **observation**, **decision**, and
**execution**. A real adapter can replace `DryRunExecutor` after adding
device-specific authentication, approval, and rollback controls. This makes
the safety boundary visible instead of hiding network changes behind a
generic "fix" function.

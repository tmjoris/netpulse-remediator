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
pytest
```

The demo simulates a degraded uplink, detects the incident, and prints the
remediation plan without changing a real device. Run `netpulse --help` to see
the available commands.

## Design notes

NetPulse intentionally separates **observation**, **decision**, and
**execution**. A real adapter can replace `DryRunExecutor` after adding
device-specific authentication, approval, and rollback controls. This makes
the safety boundary visible instead of hiding network changes behind a
generic "fix" function.

import argparse
import json
import random
from datetime import datetime, timedelta, timezone

from .engine import RemediationEngine
from .executor import DryRunExecutor
from .models import Sample


def demo(samples: int) -> None:
    engine = RemediationEngine(window=5)
    executor = DryRunExecutor("netpulse.demo.audit.jsonl")
    start = datetime.now(timezone.utc)
    for index in range(samples):
        sample = Sample(
            device="edge-dub-01",
            interface="xe-0/0/0",
            timestamp=start + timedelta(minutes=index),
            latency_ms=135 + random.uniform(-4, 4) if index >= 3 else 35,
            packet_loss_pct=12 + random.uniform(-1, 1) if index >= 3 else 0.1,
            utilization_pct=78,
        )
        incident = engine.evaluate(sample)
        if incident:
            print(json.dumps({"incident": incident.key, "severity": incident.severity.value,
                              "reason": incident.reason, "action": executor.execute(incident)["action"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Network telemetry and safe remediation lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo_parser = subparsers.add_parser("demo", help="run a deterministic degraded-link simulation")
    demo_parser.add_argument("--samples", type=int, default=12)
    serve_parser = subparsers.add_parser("serve", help="run the telemetry and incident API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "demo":
        demo(args.samples)
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("netpulse.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

"""Minimal collector client: post a batch of interface telemetry to NetPulse.

Real deployments would feed NetPulse from a streaming-telemetry pipeline
(gNMI/OpenConfig subscriptions, SNMP pollers, or active probes). This script
shows the wire format. For full failure scenarios use:

    netpulse simulate --scenario gray-failure --target http://127.0.0.1:8000

Usage: python examples/send_degraded_telemetry.py [BASE_URL]
The API token, if the server requires one, is read from $NETPULSE_API_TOKEN.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime

base_url = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
headers = {"Content-Type": "application/json"}
if token := os.environ.get("NETPULSE_API_TOKEN"):
    headers["Authorization"] = f"Bearer {token}"

for _ in range(6):
    batch = {
        "samples": [
            {
                "device": "edge-dub-01",
                "interface": "et-0/0/1",
                "timestamp": datetime.now(UTC).isoformat(),
                "latency_ms": 48.0,
                "packet_loss_pct": 14.0,
                "utilization_pct": 37.0,
            }
        ]
    }
    request = urllib.request.Request(
        f"{base_url}/v1/telemetry", data=json.dumps(batch).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request) as response:
        reply = json.loads(response.read())
    print(json.dumps({k: reply[k] for k in ("accepted", "events", "changes")}, indent=None)[:300])
    time.sleep(0.05)

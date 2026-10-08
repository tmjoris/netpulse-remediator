"""Send real HTTP telemetry to a running NetPulse instance."""

import json
import sys
import urllib.request

endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/telemetry"
for index in range(6):
    payload = {
        "device": "edge-dub-01",
        "interface": "xe-0/0/0",
        "latency_ms": 140,
        "packet_loss_pct": 12,
        "utilization_pct": 78,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        print(response.read().decode())

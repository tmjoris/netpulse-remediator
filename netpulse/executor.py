import json
from dataclasses import asdict
from pathlib import Path

from .models import Action, Incident


class DryRunExecutor:
    """Record the change that would happen, without touching a network device."""

    def __init__(self, audit_path: str | Path = "runtime/netpulse.audit.jsonl") -> None:
        self.audit_path = Path(audit_path)

    def execute(self, incident: Incident) -> dict[str, str]:
        result = {
            "status": "dry_run",
            "action": incident.action.value,
            "target": incident.key,
            "message": self._message(incident.action),
        }
        record = {"incident": asdict(incident), "result": result}
        record["incident"]["severity"] = incident.severity.value
        record["incident"]["action"] = incident.action.value
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str) + "\n")
        return result

    @staticmethod
    def _message(action: Action) -> str:
        return {
            Action.NOOP: "No change applied",
            Action.SHIFT_TRAFFIC: "Would drain traffic to a healthy path",
            Action.DISABLE_INTERFACE: "Would isolate the interface pending investigation",
        }[action]

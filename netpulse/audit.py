"""Append-only, hash-chained JSON Lines audit log.

Every record carries the SHA-256 of its predecessor, so an edited, removed or
reordered line is detectable with ``netpulse audit verify``. The log is also
the control plane's memory: on restart the engine replays it to recover the
kill-switch state, maintenance windows and drain history.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import utcnow

GENESIS_HASH = "0" * 64


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    error: str | None = None


def _digest(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_chain(path: Path) -> VerifyResult:
    """Check sequence numbers, hash links and record digests."""
    previous, expected_seq, count = GENESIS_HASH, 1, 0
    if not path.exists():
        return VerifyResult(True, 0)
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                return VerifyResult(False, count, f"line {line_no}: not valid JSON ({exc.msg})")
            if not isinstance(record, dict) or "hash" not in record:
                return VerifyResult(False, count, f"line {line_no}: not an audit record")
            if record.get("seq") != expected_seq:
                return VerifyResult(False, count, f"line {line_no}: expected seq {expected_seq}")
            if record.get("prev_hash") != previous:
                return VerifyResult(False, count, f"line {line_no}: chain broken (prev_hash mismatch)")
            if _digest(record) != record["hash"]:
                return VerifyResult(False, count, f"line {line_no}: record content does not match hash")
            previous, expected_seq, count = record["hash"], expected_seq + 1, count + 1
    return VerifyResult(True, count)


class AuditLog:
    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = self.verify()
        if not result.ok:
            raise AuditError(f"refusing to start with an invalid audit log {self.path}: {result.error}")
        for record in self.records():
            self._seq, self._last_hash = record["seq"], record["hash"]

    def append(self, kind: str, data: dict[str, Any], at: datetime) -> dict[str, Any]:
        record: dict[str, Any] = {
            "seq": self._seq + 1,
            "at": at.isoformat(),
            "logged_at": utcnow().isoformat(),
            "kind": kind,
            "data": data,
            "prev_hash": self._last_hash,
        }
        record["hash"] = _digest(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            stream.flush()
            if self.fsync:
                os.fsync(stream.fileno())
        self._seq, self._last_hash = record["seq"], record["hash"]
        return record

    def records(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)

    def verify(self) -> VerifyResult:
        return verify_chain(self.path)

    def writable(self) -> bool:
        directory = self.path.parent
        target = self.path if self.path.exists() else directory
        return directory.exists() and os.access(target, os.W_OK)

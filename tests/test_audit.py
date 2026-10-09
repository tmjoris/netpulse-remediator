import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from netpulse.audit import AuditError, AuditLog, verify_chain

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def write_three(path: Path) -> AuditLog:
    log = AuditLog(path, fsync=False)
    for index in range(3):
        log.append("test", {"n": index}, NOW)
    return log


def test_chain_verifies_and_continues_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_three(path)
    AuditLog(path, fsync=False).append("test", {"n": 3}, NOW)
    result = verify_chain(path)
    assert result.ok and result.records == 4


def test_edited_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_three(path)
    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["data"]["n"] = 99
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    result = verify_chain(path)
    assert not result.ok and "line 2" in (result.error or "")


def test_deleted_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_three(path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not verify_chain(path).ok


def test_refuses_to_start_on_corrupt_log(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_three(path)
    with path.open("a") as stream:
        stream.write('{"truncated": ')
    with pytest.raises(AuditError):
        AuditLog(path)


def test_writable(tmp_path: Path) -> None:
    assert AuditLog(tmp_path / "nested" / "audit.jsonl").writable()

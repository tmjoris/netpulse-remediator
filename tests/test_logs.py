import json
import logging

import pytest

from netpulse import logs


def test_json_logs_carry_structured_fields(capsys: pytest.CaptureFixture[str]) -> None:
    logs.configure("json", "info")
    logging.getLogger("netpulse.test").info("drained %s", "r1:et-0", extra={"change_id": "CHG-000001"})
    entry = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert entry["msg"] == "drained r1:et-0"
    assert entry["change_id"] == "CHG-000001"
    assert entry["level"] == "info"
    logs.configure("text", "warning")
    assert logging.getLogger().level == logging.WARNING

import json
from pathlib import Path

import pytest

from netpulse.cli import main

EXAMPLE = str(Path(__file__).parent.parent / "config" / "netpulse.example.toml")


def test_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "drain -> applied" in out and "undrain -> applied" in out


def test_simulate_list_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate", "--list"]) == 0
    assert "flapping" in capsys.readouterr().out
    assert main(["simulate", "--scenario", "insufficient-capacity", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scenario"] == "insufficient-capacity"


def test_simulate_keeps_verifiable_audit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate", "--scenario", "gray-failure", "--audit", str(tmp_path)]) == 0
    audit = tmp_path / "gray-failure.audit.jsonl"
    assert main(["audit", "verify", str(audit)]) == 0
    lines = audit.read_text().splitlines()
    audit.write_text("\n".join(lines[1:]) + "\n")
    assert main(["audit", "verify", str(audit)]) == 1
    assert main(["audit", "verify", str(tmp_path / "missing.jsonl")]) == 1


def test_check_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", EXAMPLE]) == 0
    assert "link groups=2" in capsys.readouterr().out
    bad = tmp_path / "bad.toml"
    bad.write_text("[detection]\nwindow = 1\n")
    assert main(["check-config", str(bad)]) == 1


def test_simulate_rehearses_a_policy_change(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "policy.toml"
    # The survivor would still be at ~119%, so even a 100% limit blocks the drain.
    config.write_text("[safety]\nmax_post_drain_utilization_pct = 100.0\n")
    assert main(["simulate", "--scenario", "insufficient-capacity", "--config", str(config)]) == 0
    assert "limit 100%" in capsys.readouterr().out
    # A 50% critical threshold means 12% loss only warns, so there is nothing to drain.
    config.write_text("[detection]\nloss_critical_pct = 50.0\n")
    assert main(["simulate", "--scenario", "insufficient-capacity", "--config", str(config)]) == 0
    assert "drains: 0, undrains: 0, blocked: 0" in capsys.readouterr().out
    assert main(["simulate", "--config", str(config), "--target", "http://127.0.0.1:1"]) == 2

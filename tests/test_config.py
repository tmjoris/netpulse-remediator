from datetime import timedelta
from pathlib import Path

import pytest

from netpulse.config import ConfigError, load_settings, parse_settings

EXAMPLE = Path(__file__).parent.parent / "config" / "netpulse.example.toml"


def test_example_config_loads() -> None:
    settings = load_settings(EXAMPLE, env={})
    assert len(settings.topology) == 2
    assert settings.safety.undrain_soak == timedelta(minutes=15)
    assert settings.executor == "dry_run"


def test_environment_overrides_file(tmp_path: Path) -> None:
    env = {"NETPULSE_API_TOKEN": "s3cret", "NETPULSE_AUDIT_PATH": str(tmp_path / "a.jsonl")}
    settings = load_settings(EXAMPLE, env=env)
    assert settings.api_token == "s3cret"
    assert settings.audit_path == tmp_path / "a.jsonl"


def test_defaults_without_a_file() -> None:
    settings = load_settings(None, env={})
    assert settings.source == "<defaults>" and len(settings.topology) == 0


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"detection": {"windw": 5}}, "unknown key detection.windw"),
        ({"detection": {"window": "5"}}, "must be of type int"),
        ({"detection": {"breach_count": 9}}, "breach_count"),
        ({"detection": {"loss_warning_pct": 20}}, "loss_warning_pct < loss_critical_pct"),
        ({"safety": {"undrain_soak_s": -1}}, "non-negative"),
        ({"safety": {"undrain_soak": 5}}, "unknown key safety.undrain_soak"),
        ({"safety": {"automation_enabled": 1}}, "must be of type bool"),
        ({"executor": {"mode": "live"}}, "executor.mode"),
        ({"surprise": {}}, "unknown top-level"),
        ({"link_groups": [{"name": "g", "members": [{"device": "a"}]}]}, "link_groups[0]"),
        (
            {
                "link_groups": [
                    {"name": "g", "members": [{"device": "a", "interface": "x", "capacity_gbps": 1}]}
                ]
            },
            "at least two members",
        ),
    ],
)
def test_invalid_config_is_rejected(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_settings(data)


def test_interface_in_two_groups_is_rejected() -> None:
    member = {"device": "a", "interface": "x", "capacity_gbps": 100}
    other = {"device": "a", "interface": "y", "capacity_gbps": 100}
    groups = [{"name": "g1", "members": [member, other]}, {"name": "g2", "members": [member, other]}]
    with pytest.raises(ConfigError, match="appears in both"):
        parse_settings({"link_groups": groups})


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", env={})
    bad = tmp_path / "bad.toml"
    bad.write_text("[detection\n")
    with pytest.raises(ConfigError):
        load_settings(bad, env={})


LAB = Path(__file__).parent.parent / "lab" / "frr" / "netpulse.toml"


def test_frr_lab_config_loads() -> None:
    from netpulse.models import InterfaceRef

    settings = load_settings(LAB, env={})
    assert settings.executor == "frr"
    assert settings.devices.hosts == {"r1": "netpulse-lab-r1", "r2": "netpulse-lab-r2"}
    assert settings.frr.peers[InterfaceRef("r1", "lnk1")] == InterfaceRef("r2", "lnk1")
    assert [p.target for p in settings.probes] == ["10.0.1.3", "10.0.2.3"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"executor": {"mode": "frr"}}, "needs \\[devices.hosts\\]"),
        ({"executor": {"frr": {"drain_cost": 5}}}, "normal_cost < drain_cost"),
        ({"executor": {"frr": {"peers": {"r1": "r2:x"}}}}, "executor.frr.peers"),
        ({"executor": {"extra": 1}}, "unknown key\\(s\\) in executor"),
        ({"devices": {"transport": "telnet"}}, "devices.transport"),
        ({"devices": {"hosts": {"r1": 5}}}, "devices.hosts"),
        ({"probes": [{"device": "r1", "interface": "x", "target": "1.1.1.1"}]}, "not in \\[devices.hosts\\]"),
        ({"probes": [{"device": "r1"}]}, "probes: missing"),
    ],
)
def test_invalid_frr_config(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_settings(data)

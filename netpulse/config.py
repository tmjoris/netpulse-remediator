"""Policy, safety and topology configuration loaded from TOML plus environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import InterfaceRef
from .topology import LinkGroup, Member, Topology


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DetectionPolicy:
    window: int = 5
    breach_count: int = 4  # samples in the window that must breach (M-of-N)
    latency_warning_ms: float = 100.0
    loss_warning_pct: float = 2.0
    loss_critical_pct: float = 10.0
    # Hysteresis: an incident clears only once samples are clearly healthy,
    # not merely back under the alerting threshold.
    latency_clear_ms: float = 80.0
    loss_clear_pct: float = 0.5

    def validate(self) -> None:
        if self.window < 2:
            raise ConfigError("detection.window must be at least 2")
        if not 1 <= self.breach_count <= self.window:
            raise ConfigError("detection.breach_count must be between 1 and detection.window")
        if not 0 < self.loss_warning_pct < self.loss_critical_pct <= 100:
            raise ConfigError("detection: require 0 < loss_warning_pct < loss_critical_pct <= 100")
        if self.loss_clear_pct > self.loss_warning_pct:
            raise ConfigError("detection.loss_clear_pct must not exceed loss_warning_pct")
        if self.latency_clear_ms > self.latency_warning_ms:
            raise ConfigError("detection.latency_clear_ms must not exceed latency_warning_ms")


@dataclass(frozen=True)
class SafetyPolicy:
    automation_enabled: bool = True
    max_concurrent_drains: int = 2  # fleet-wide blast-radius limit
    max_post_drain_utilization_pct: float = 80.0
    min_active_members: int = 1
    action_cooldown: timedelta = timedelta(minutes=10)
    undrain_soak: timedelta = timedelta(minutes=15)
    flap_max_drains: int = 3
    flap_window: timedelta = timedelta(hours=24)
    utilization_max_age: timedelta = timedelta(minutes=5)

    def validate(self) -> None:
        if self.max_concurrent_drains < 1:
            raise ConfigError("safety.max_concurrent_drains must be at least 1")
        if not 0 < self.max_post_drain_utilization_pct <= 100:
            raise ConfigError("safety.max_post_drain_utilization_pct must be in (0, 100]")
        if self.min_active_members < 1:
            raise ConfigError("safety.min_active_members must be at least 1")
        if self.flap_max_drains < 1:
            raise ConfigError("safety.flap_max_drains must be at least 1")


EXECUTOR_MODES = ("dry_run", "lab")


@dataclass(frozen=True)
class Settings:
    detection: DetectionPolicy = field(default_factory=DetectionPolicy)
    safety: SafetyPolicy = field(default_factory=SafetyPolicy)
    topology: Topology = field(default_factory=Topology)
    executor: str = "dry_run"
    audit_path: Path = Path("runtime/netpulse.audit.jsonl")
    api_token: str | None = None
    source: str = "<defaults>"

    def validate(self) -> Settings:
        self.detection.validate()
        self.safety.validate()
        if self.executor not in EXECUTOR_MODES:
            raise ConfigError(f"executor.mode must be one of {EXECUTOR_MODES}")
        return self


# TOML keys ending in _s are durations in seconds.
_SAFETY_DURATIONS = {
    "action_cooldown_s": "action_cooldown",
    "undrain_soak_s": "undrain_soak",
    "flap_window_s": "flap_window",
    "utilization_max_age_s": "utilization_max_age",
}


def _check_type(key: str, default: Any, value: Any) -> None:
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = True
    if not ok:
        raise ConfigError(f"{key} must be of type {type(default).__name__}, got {value!r}")


def _build(cls: type, section: dict[str, Any], name: str, renames: dict[str, str] | None = None) -> Any:
    renames = renames or {}
    allowed = {f.name for f in fields(cls)}
    values: dict[str, Any] = {}
    for key, value in section.items():
        if key in renames:
            if not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"{name}.{key} must be a non-negative number of seconds")
            values[renames[key]] = timedelta(seconds=value)
        elif key in allowed and key not in renames.values():
            _check_type(f"{name}.{key}", getattr(cls(), key), value)
            values[key] = value
        else:
            raise ConfigError(f"unknown key {name}.{key}")
    try:
        return cls(**values)
    except TypeError as exc:  # pragma: no cover - guarded by key validation above
        raise ConfigError(f"invalid {name} section: {exc}") from exc


def _topology(groups: list[dict[str, Any]]) -> Topology:
    built = []
    for index, group in enumerate(groups):
        try:
            members = tuple(
                Member(InterfaceRef(m["device"], m["interface"]), float(m["capacity_gbps"]))
                for m in group["members"]
            )
            built.append(LinkGroup(group["name"], members))
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"link_groups[{index}] is missing or has an invalid field: {exc}") from exc
    try:
        return Topology(built)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def parse_settings(data: dict[str, Any], source: str = "<inline>") -> Settings:
    known = {"detection", "safety", "executor", "audit", "api", "link_groups"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown top-level section(s): {', '.join(sorted(unknown))}")
    settings = Settings(
        detection=_build(DetectionPolicy, data.get("detection", {}), "detection"),
        safety=_build(SafetyPolicy, data.get("safety", {}), "safety", _SAFETY_DURATIONS),
        topology=_topology(data.get("link_groups", [])),
        executor=data.get("executor", {}).get("mode", "dry_run"),
        audit_path=Path(data.get("audit", {}).get("path", "runtime/netpulse.audit.jsonl")),
        api_token=data.get("api", {}).get("token"),
        source=source,
    )
    return settings.validate()


def load_settings(path: str | Path | None = None, env: dict[str, str] | None = None) -> Settings:
    """Load settings from ``path`` (or $NETPULSE_CONFIG), then apply env overrides.

    Secrets such as the API token should come from the environment rather than
    the config file.
    """
    env = dict(os.environ) if env is None else env
    path = path or env.get("NETPULSE_CONFIG")
    if path:
        try:
            with open(path, "rb") as stream:
                data = tomllib.load(stream)
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        settings = parse_settings(data, str(path))
    else:
        settings = Settings()

    overrides: dict[str, Any] = {}
    if env.get("NETPULSE_API_TOKEN"):
        overrides["api_token"] = env["NETPULSE_API_TOKEN"]
    if env.get("NETPULSE_AUDIT_PATH"):
        overrides["audit_path"] = Path(env["NETPULSE_AUDIT_PATH"])
    if env.get("NETPULSE_EXECUTOR"):
        overrides["executor"] = env["NETPULSE_EXECUTOR"]
    return replace(settings, **overrides).validate() if overrides else settings

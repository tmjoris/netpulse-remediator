"""Command-line entry point: serve, simulate, demo, check-config, audit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__
from .audit import AuditError, verify_chain
from .config import ConfigError, load_settings
from .models import to_jsonable, utcnow
from .safety import human
from .simulator import SCENARIOS, HttpSink, InProcessSink, format_record, lab_engine, run_scenario


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from . import logs
    from .server import create_app

    logs.configure(args.log_format, args.log_level)
    try:
        app = create_app(load_settings(args.config))
    except (ConfigError, AuditError) as exc:
        print(f"netpulse: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=args.access_log)
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    if args.list:
        for scenario in SCENARIOS.values():
            print(f"{scenario.name:<22} {scenario.summary}\n{'':<22} expect: {scenario.expect}")
        return 0
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    policy = None
    if args.config:
        if args.target:
            print("netpulse: --config applies to in-process runs only", file=sys.stderr)
            return 2
        try:
            policy = load_settings(args.config, env={})
        except ConfigError as exc:
            print(f"netpulse: {exc}", file=sys.stderr)
            return 2
    for name in names:
        scenario = SCENARIOS[name]
        start = None
        if args.target:
            if scenario.executor_faults:
                print(f"note: {name} injects executor faults, which only works in-process", file=sys.stderr)
            http = HttpSink(args.target, args.token or os.environ.get("NETPULSE_API_TOKEN"))
            # Event time must keep moving forward for a long-running server.
            latest = http.latest_timestamp()
            start = max(utcnow(), latest + timedelta(minutes=1)) if latest else utcnow()
            sink: HttpSink | InProcessSink = http
        else:
            audit_path = Path(args.audit) / f"{name}.audit.jsonl" if args.audit else None
            if audit_path and audit_path.exists():
                audit_path.unlink()
            sink = InProcessSink(lab_engine(scenario, audit_path, policy))

        if not args.json:
            print(f"\n== {scenario.name}: {scenario.summary}\n   expect: {scenario.expect}\n")
        report = run_scenario(scenario, sink, seed=args.seed, start=start)
        if args.json:
            print(json.dumps({"scenario": name, "records": report.records}, default=str))
            continue
        for record in report.records:
            for line in format_record(record, report.start):
                print(line)
        print(
            f"\n   incidents opened: {report.count('opened')}, "
            f"drains: {len(report.changes('drain', 'applied', 'dry_run'))}, "
            f"undrains: {len(report.changes('undrain', 'applied', 'dry_run'))}, "
            f"blocked: {len(report.changes('drain', 'blocked') + report.changes('undrain', 'blocked'))}, "
            f"still drained: {', '.join(sorted(map(str, report.drained_at_end))) or 'none'}"
        )
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    members = sum(len(g.members) for g in settings.topology.groups)
    print(f"ok: {settings.source}")
    print(f"  executor={settings.executor} audit={settings.audit_path}")
    print(f"  link groups={len(settings.topology)} members={members}")
    print(f"  detection={json.dumps(to_jsonable(settings.detection))}")
    safety = {k: human(v) if isinstance(v, timedelta) else v for k, v in vars(settings.safety).items()}
    print(f"  safety={json.dumps(safety)}")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"FAIL {path}: no such file", file=sys.stderr)
        return 1
    result = verify_chain(path)
    if not result.ok:
        print(f"FAIL {path}: {result.error} (after {result.records} valid record(s))", file=sys.stderr)
        return 1
    print(f"ok: {path}: {result.records} record(s), hash chain intact")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="netpulse", description=__doc__)
    parser.add_argument("--version", action="version", version=f"netpulse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the telemetry and remediation API")
    serve.add_argument("--config", help="TOML config (default: $NETPULSE_CONFIG)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-format", choices=["text", "json"], default="text")
    serve.add_argument("--log-level", default="INFO")
    serve.add_argument("--access-log", action="store_true", help="log every HTTP request")
    serve.set_defaults(func=cmd_serve)

    simulate = sub.add_parser("simulate", help="run a failure scenario against the engine or a live server")
    simulate.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    simulate.add_argument("--list", action="store_true", help="describe the scenarios and exit")
    simulate.add_argument("--seed", type=int, default=7)
    simulate.add_argument("--target", help="NetPulse base URL, e.g. http://127.0.0.1:8000")
    simulate.add_argument("--token", help="API token for --target (default: $NETPULSE_API_TOKEN)")
    simulate.add_argument("--audit", help="directory to keep in-process audit logs in")
    simulate.add_argument(
        "--config", help="rehearse this config's detection/safety policy on the lab topology (in-process)"
    )
    simulate.add_argument("--json", action="store_true", help="emit machine-readable records")
    simulate.set_defaults(func=cmd_simulate)

    demo = sub.add_parser("demo", help="shortcut for: simulate --scenario gray-failure")
    demo.set_defaults(
        func=cmd_simulate,
        config=None,
        scenario="gray-failure",
        list=False,
        seed=7,
        target=None,
        token=None,
        audit=None,
        json=False,
    )

    check = sub.add_parser("check-config", help="validate a config file and print the effective policy")
    check.add_argument("config", nargs="?", help="TOML config (default: $NETPULSE_CONFIG)")
    check.set_defaults(func=cmd_check_config)

    audit = sub.add_parser("audit", help="audit log tools")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    verify = audit_sub.add_parser("verify", help="check the audit log hash chain")
    verify.add_argument("path", nargs="?", default="runtime/netpulse.audit.jsonl")
    verify.set_defaults(func=cmd_audit_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

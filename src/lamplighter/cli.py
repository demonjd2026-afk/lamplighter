"""Command line entry point: `lamplighter run | audit | show <incident-id>`."""
from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
from dataclasses import dataclass

from .actions import ControlPlaneClient
from .config import LOOPBACK_HOSTS, ConfigError, Settings
from .llm import LLMClient, client_from
from .orchestrator import Orchestrator
from .policy import Policy, PolicyError
from .store import Store
from .telemetry import JsonlTelemetry


@dataclass
class Runtime:
    settings: Settings
    store: Store
    telemetry: JsonlTelemetry
    orchestrator: Orchestrator


def build(settings: Settings, llm: LLMClient | None = None) -> Runtime:
    store = Store(settings.db_path)
    telemetry = JsonlTelemetry(settings.data_dir, retention_s=max(7200.0, settings.timing.lookback_s * 2))
    actuator = ControlPlaneClient(settings.controlplane_url, settings.controlplane_token, settings.allowed_hosts)
    policy = Policy.load(settings.policy_file)
    orchestrator = Orchestrator(settings, store, telemetry, actuator, policy, llm or client_from(settings))
    return Runtime(settings, store, telemetry, orchestrator)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lamplighter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the worker and the API")
    sub.add_parser("audit", help="verify the audit chain")
    show = sub.add_parser("show", help="print an incident's postmortem")
    show.add_argument("incident_id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.from_env()
        if args.command == "audit":
            intact, entries, bad = Store(settings.db_path).verify_audit()
            print(f"audit chain intact over {entries} entries" if intact else f"audit chain BROKEN at entry {bad}")
            return 0 if intact else 1
        if args.command == "show":
            path = settings.data_dir / "incidents" / args.incident_id / "postmortem.md"
            if not re.fullmatch(r"inc-\d{8}-\d{6}-[0-9a-f]{6}", args.incident_id) or not path.is_file():
                print("no postmortem for that incident", file=sys.stderr)
                return 1
            print(path.read_text(encoding="utf-8"))
            return 0
        if settings.public_read and settings.bind_host not in LOOPBACK_HOSTS:
            raise ConfigError("LAMPLIGHTER_PUBLIC_READ is only allowed when bound to loopback")
        runtime = build(settings)
    except (ConfigError, PolicyError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    from .api import create_app

    stop = threading.Event()
    worker = threading.Thread(target=runtime.orchestrator.run, args=(stop,), daemon=True, name="worker")
    worker.start()
    try:
        uvicorn.run(create_app(settings, runtime.store, runtime.telemetry, runtime.orchestrator),
                    host=settings.bind_host, port=settings.bind_port, log_level="warning", access_log=False)
    finally:
        stop.set()
        worker.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())

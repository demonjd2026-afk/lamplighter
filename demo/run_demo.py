"""Run the demo: the shop, live traffic, Lamplighter and its dashboard, then break things.

    python -m demo.run_demo                       # tour: bad release, bad config, hung dependency
    python -m demo.run_demo --scenario bad_release
    python -m demo.run_demo --mode propose        # wait for a human to approve each plan
    python -m demo.run_demo --hold                # keep everything running afterwards

Optional model (narrative, and a sandbox-verified patch suggestion with --patch):
    export LAMPLIGHTER_LLM_PROVIDER=anthropic LAMPLIGHTER_LLM_MODEL=<model id> LAMPLIGHTER_LLM_API_KEY=<key>
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import uvicorn

from eval.lab import PROJECT_ROOT, Lab
from eval.scenarios import BY_NAME
from lamplighter.api import create_app
from lamplighter.config import LOOPBACK_HOSTS, ConfigError, validate_url
from lamplighter.llm import AnthropicClient, LLMClient, OpenAICompatibleClient

TOUR = ["bad_release", "bad_config", "dependency_stuck"]


def model_from_env() -> LLMClient | None:
    provider = os.environ.get("LAMPLIGHTER_LLM_PROVIDER", "").lower()
    if not provider:
        return None
    model, key = os.environ.get("LAMPLIGHTER_LLM_MODEL", ""), os.environ.get("LAMPLIGHTER_LLM_API_KEY", "")
    if provider not in ("anthropic", "openai") or not model or not key:
        raise ConfigError("set LAMPLIGHTER_LLM_PROVIDER (anthropic|openai), LAMPLIGHTER_LLM_MODEL and LAMPLIGHTER_LLM_API_KEY")
    default = "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com/v1"
    base = os.environ.get("LAMPLIGHTER_LLM_BASE_URL", default)
    base = validate_url(base, LOOPBACK_HOSTS | {urlparse(base).hostname or ""}, "LAMPLIGHTER_LLM_BASE_URL")
    if provider == "anthropic":
        return AnthropicClient(base, key, model, os.environ.get("LAMPLIGHTER_ANTHROPIC_VERSION", "2026-10-21"))
    return OpenAICompatibleClient(base, key, model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(BY_NAME), action="append", help="repeatable; default is a tour")
    parser.add_argument("--mode", choices=["auto", "propose", "observe"], default="auto")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--patch", action="store_true", help="let the model propose a patch (verified in a sandbox)")
    parser.add_argument("--hold", action="store_true", help="keep running after the scenarios finish")
    parser.add_argument("--keep", action="store_true", help="keep the data directory")
    args = parser.parse_args(argv)
    try:
        model = model_from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    data_dir = Path(tempfile.mkdtemp(prefix="lamplighter-demo-"))
    policy = data_dir / "policy.toml"
    policy.write_text((PROJECT_ROOT / "demo" / "policy.toml").read_text(encoding="utf-8").replace(
        'mode = "auto"', f'mode = "{args.mode}"'), encoding="utf-8")
    api_token = secrets.token_urlsafe(24)
    lab = Lab(data_dir, policy_file=policy, llm=model, llm_patch=args.patch, api_token=api_token, public_read=True)
    if args.mode == "propose":
        lab.settings = replace(lab.settings, timing=replace(lab.settings.timing, approval_ttl_s=600))
    server = None
    try:
        print("starting the shop, traffic and Lamplighter ...", flush=True)
        lab.start(warmup_s=0)
        app = create_app(lab.settings, lab.runtime.store, lab.runtime.telemetry, lab.runtime.orchestrator)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        print(f"\n  dashboard: http://127.0.0.1:{args.port}\n  mode: {args.mode}   model: {'on' if model else 'off'}")
        if args.mode == "propose":
            print(f"  API token for approvals (paste it into the dashboard): {api_token}")
        print("\nwarming up with healthy traffic (20 s) ...", flush=True)
        time.sleep(20)
        for name in args.scenario or TOUR:
            scenario = BY_NAME[name]
            before = {i["id"] for i in lab.incidents()}
            print(f"\n=== {scenario.title} ({name}) ===", flush=True)
            started = scenario.inject(lab)
            deadline = time.monotonic() + (scenario.watch_s or (700 if args.mode == "propose" else 120))
            incident = None
            while time.monotonic() < deadline:
                fresh = [i for i in lab.incidents() if i["id"] not in before]
                if fresh and fresh[-1]["closed_at"]:
                    incident = lab._after_post_close(fresh[-1])
                    break
                time.sleep(0.5)
            if incident is None:
                print("no incident was closed in the time allowed (for some scenarios that is the right answer)")
            else:
                outcome = incident["data"]["outcome"]
                print(f"{incident['id']}: {incident['state']} - {outcome['reason']}")
                print(f"customer impact: {lab.customer_impact(started)['impact_s']} s")
                print(f"postmortem: {data_dir / 'incidents' / incident['id'] / 'postmortem.md'}")
            lab.plane.inject_fault("inventory", "none")
            lab.plane.inject_fault("payments", "none")
            time.sleep(35)  # let the windows clear and the cooldown pass before the next fault
        if args.hold:
            print("\nholding; press Ctrl+C to stop", flush=True)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.should_exit = True
        lab.stop()
        if args.keep:
            print(f"data kept in {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build a launchd plist artifact for hermes-router. Builder only: never installs,
never bootstraps, never touches an existing service."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
from pathlib import Path

LABEL = "com.hermes.router"
RUNTIME_HOME = Path.home() / "codex/hermes-router"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate-launchd.py",
        description="Generate a launchd plist for hermes-router (artifact only).",
    )
    parser.add_argument("--output", required=True, help="plist path to write")
    parser.add_argument("--data-dir", required=True, help="router data directory")
    parser.add_argument(
        "--repository",
        action="append",
        default=[],
        required=True,
        help="allowed repository root; repeatable",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="allow real providers (default: disabled)",
    )
    parser.add_argument("--root", default=None, help="repo root (default: parent of scripts/)")
    return parser.parse_args(argv)


def build(root: Path, data_dir: Path, repositories: list[str], allow_real: bool) -> dict:
    logs = data_dir.parent / "logs"
    python = root / ".venv/bin/python"
    env = {
        "HERMES_ROUTER_DATA_DIR": str(data_dir),
        "HERMES_ROUTER_ALLOWED_REPOS": json.dumps(repositories),
        "HERMES_ROUTER_ALLOW_REAL": "1" if allow_real else "0",
        "HERMES_ROUTER_HERMES": str(Path.home() / ".local/bin/hermes"),
        "HERMES_ROUTER_LOG_DIR": str(logs),
        "PATH": str(root / ".venv/bin") + ":" + str(Path.home() / ".local/bin") + ":/opt/homebrew/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
    }
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "hermes_router", "--host", "127.0.0.1", "--port", "8080"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "hermes-router.out.log"),
        "StandardErrorPath": str(logs / "hermes-router.err.log"),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    data_dir = Path(args.data_dir).expanduser().resolve()
    plist = build(root, data_dir, [str(Path(r).expanduser().resolve()) for r in args.repository], args.real)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (data_dir.parent / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        plistlib.dump(plist, handle)
    print(f"wrote {output} (allow_real={plist['EnvironmentVariables']['HERMES_ROUTER_ALLOW_REAL']})")
    print("not installed; review then: plutil -lint %s" % output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

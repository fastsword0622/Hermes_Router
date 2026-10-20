"""``python -m hermes_router`` / ``hermes-router`` entrypoint."""

from __future__ import annotations

import argparse
import sys
import os
from dataclasses import replace

from .config import Settings
from .registry import list_specs


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="hermes-router",
        description="Hermes Development Router local task API server.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--log-level", default="info", choices=["critical", "error", "warning", "info"]
    )
    parser.add_argument(
        "--providers",
        action="store_true",
        help="print the provider registry and exit",
    )
    args = parser.parse_args(argv)

    if args.providers:
        for spec in list_specs():
            print(f"{spec.name:10} {spec.provider_id:20} {spec.model_id:20} real={spec.is_real}")
        return 0

    import uvicorn

    from .api import create_app

    settings = Settings.from_env()
    if args.data_dir:
        settings = replace(settings, data_dir=type(settings.data_dir)(args.data_dir),
            task_db_path=None, checkpoint_db_path=None, log_dir=None)

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

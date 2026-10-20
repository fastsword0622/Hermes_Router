"""Runtime configuration.

Every filesystem location the router writes to is configurable so that caches,
logs, coverage and temp data can be kept outside the repository and outside the
Hermes runtime installation.  Nothing here points at ``~/.hermes`` and nothing
here mutates Hermes configuration.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path

from .registry import DEFAULT_PROVIDER

__all__ = ["Settings", "get_settings", "DEFAULT_DATA_ROOT", "reset_settings"]

#: Default scratch root for runtime state (databases).  Overridable via
#: ``HERMES_ROUTER_DATA_DIR`` so tests and operators can redirect it.
DEFAULT_DATA_ROOT = Path(
    os.environ.get(
        "HERMES_ROUTER_DATA_DIR",
        str(Path.home() / "codex" / "hermes-router" / "data"),
    )
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    """Phase 1 settings."""

    data_dir: Path = DEFAULT_DATA_ROOT
    task_db_path: Path = field(default=None)  # type: ignore[assignment]
    checkpoint_db_path: Path = field(default=None)  # type: ignore[assignment]
    log_dir: Path = field(default=None)  # type: ignore[assignment]

    #: Default provider name.  Phase 1 pins this to ``mock``; changing it to a
    #: real provider name has no effect unless a matching adapter is injected.
    default_provider: str = DEFAULT_PROVIDER

    #: Bounds applied to every task unless overridden per request.
    default_max_attempts: int = 1
    default_timeout_seconds: float = 60.0
    default_max_turns: int = 1

    #: How long to wait for the worker thread pool to drain on shutdown.
    worker_shutdown_timeout: float = 10.0

    #: Set to True only when an operator explicitly wires a real Hermes adapter.
    allow_real_providers: bool = False

    version: str = "0.1.0"
    allowed_repositories: tuple[Path, ...] = ()
    hermes_executable: str = "hermes"
    hermes_credentials: Path | None = None
    api_token: str | None = None
    max_request_bytes: int = 2_000_000
    log_max_bytes: int = 1_000_000
    log_backup_count: int = 5

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if self.task_db_path is None:
            self.task_db_path = self.data_dir / "tasks.sqlite3"
        if self.checkpoint_db_path is None:
            self.checkpoint_db_path = self.data_dir / "checkpoints.sqlite3"
        if self.log_dir is None:
            self.log_dir = self.data_dir / "logs"
        self.task_db_path = Path(self.task_db_path)
        self.checkpoint_db_path = Path(self.checkpoint_db_path)
        self.log_dir = Path(self.log_dir)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.task_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("HERMES_ROUTER_DATA_DIR", str(DEFAULT_DATA_ROOT)))
        return cls(
            data_dir=data_dir,
            task_db_path=Path(
                os.environ.get("HERMES_ROUTER_TASK_DB", data_dir / "tasks.sqlite3")
            ),
            checkpoint_db_path=Path(
                os.environ.get(
                    "HERMES_ROUTER_CHECKPOINT_DB", data_dir / "checkpoints.sqlite3"
                )
            ),
            log_dir=Path(
                os.environ.get("HERMES_ROUTER_LOG_DIR", data_dir / "logs")
            ),
            default_provider=os.environ.get(
                "HERMES_ROUTER_DEFAULT_PROVIDER", DEFAULT_PROVIDER
            ),
            default_max_attempts=_env_int("HERMES_ROUTER_MAX_ATTEMPTS", 1),
            default_timeout_seconds=_env_float("HERMES_ROUTER_TIMEOUT", 60.0),
            default_max_turns=_env_int("HERMES_ROUTER_MAX_TURNS", 1),
            worker_shutdown_timeout=_env_float("HERMES_ROUTER_SHUTDOWN_TIMEOUT", 10.0),
            allow_real_providers=os.environ.get(
                "HERMES_ROUTER_ALLOW_REAL", ""
            ).lower()
            in {"1", "true", "yes"},
            allowed_repositories=tuple(Path(p).resolve() for p in json.loads(os.environ.get("HERMES_ROUTER_ALLOWED_REPOS", "[]"))),
            hermes_executable=os.environ.get("HERMES_ROUTER_HERMES", "hermes"),
            hermes_credentials=Path(os.environ["HERMES_ROUTER_CREDENTIALS"]) if os.environ.get("HERMES_ROUTER_CREDENTIALS") else None,
            api_token=os.environ.get("HERMES_ROUTER_API_TOKEN"),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings (used by tests that change the environment)."""
    global _settings
    _settings = None

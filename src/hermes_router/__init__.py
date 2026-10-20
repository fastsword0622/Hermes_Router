"""Hermes Development Router -- Phase 1."""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Settings, get_settings
from .models import (
    AttemptResult,
    CreateTaskRequest,
    ErrorClass,
    Task,
    TaskStatus,
)
from .registry import DEFAULT_PROVIDER, get_spec, list_specs

__all__ = [
    "__version__",
    "Settings",
    "get_settings",
    "Task",
    "TaskStatus",
    "AttemptResult",
    "ErrorClass",
    "CreateTaskRequest",
    "DEFAULT_PROVIDER",
    "get_spec",
    "list_specs",
]
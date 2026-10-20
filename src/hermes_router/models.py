"""Pydantic v2 domain models and API schemas for Phase 1."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .registry import DEFAULT_PROVIDER

__all__ = [
    "TaskStatus",
    "ErrorClass",
    "AttemptResult",
    "Task",
    "CreateTaskRequest",
    "TaskCreated",
    "TaskView",
    "TaskList",
    "ProviderInfo",
    "HealthResponse",
    "new_task_id",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_task_id() -> uuid.UUID:
    return uuid.uuid4()


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Task was persisted mid-flight (queued/running) when the process died.
    #: Recovered to this state on restart; never auto-executed against a real
    #: provider.  See service.recover_orphaned_tasks.
    INTERRUPTED = "interrupted"
    AWAITING_VERIFICATION = "awaiting_verification"


#: Statuses that mean work is still outstanding.
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.QUEUED, TaskStatus.RUNNING}
)

#: Statuses that will never change again.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
    }
)


class ErrorClass(str, Enum):
    """Classification of why an attempt did not succeed."""

    NONE = "none"
    CLEANUP_FAILED = "cleanup_failed"
    INPUT_TOO_LARGE = "input_too_large"
    #: Process exited with a nonzero code.
    EXIT_CODE = "exit_code"
    #: Parent wall-clock deadline elapsed; whole process group was killed.
    TIMEOUT = "timeout"
    #: A bounded output limit was hit.
    OUTPUT_TRUNCATED = "output_truncated"
    #: Executable could not be spawned.
    SPAWN_FAILED = "spawn_failed"
    #: Anything else unexpected inside the adapter.
    ADAPTER_ERROR = "adapter_error"
    #: Attempt budget exhausted.
    MAX_ATTEMPTS = "max_attempts"
    #: Task wall-clock deadline exhausted across all attempts.
    DEADLINE_EXCEEDED = "deadline_exceeded"
    #: Task cancelled before/while running.
    CANCELLED = "cancelled"


#: Error classes that mean "retrying may plausibly help".
RETRYABLE_ERRORS: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.TIMEOUT,
        ErrorClass.EXIT_CODE,
        ErrorClass.SPAWN_FAILED,
        ErrorClass.ADAPTER_ERROR,
        ErrorClass.DEADLINE_EXCEEDED,
    }
)


class AttemptResult(BaseModel):
    """Structured result of one provider attempt.

    Invariant enforced by providers and re-verified here: ``stdout``/``stderr``
    are always bounded in length and always redacted.
    """

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1, description="1-based attempt number within the task.")
    provider: str
    provider_id: str
    model_id: str
    ok: bool = Field(description="Provider-reported success, not merely exit code 0.")
    exit_code: int | None = None
    stdout: str = Field(default="", description="Redacted, length-bounded stdout.")
    stderr: str = Field(default="", description="Redacted, length-bounded stderr.")
    error_class: ErrorClass = ErrorClass.NONE
    error_message: str | None = None
    duration_seconds: float = Field(default=0.0, ge=0.0)
    truncated: bool = Field(default=False, description="Output hit the capture bound.")
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    @field_validator("stdout", "stderr")
    @classmethod
    def _bound_output(cls, value: str) -> str:
        from .providers.base import MAX_CAPTURE_BYTES
        from .redaction import redact_text

        value = redact_text(value)

        if len(value.encode("utf-8")) <= MAX_CAPTURE_BYTES:
            return value
        encoded = value.encode("utf-8")[:MAX_CAPTURE_BYTES]
        return encoded.decode("utf-8", errors="ignore")


class Task(BaseModel):
    """Durable task record."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("prompt", "error_message", "result_summary")
    @classmethod
    def _redact(cls, value: str | None) -> str | None:
        from .redaction import redact_text
        return redact_text(value) if value is not None else None

    id: uuid.UUID = Field(default_factory=new_task_id)
    prompt: str = Field(min_length=1)
    provider: str = DEFAULT_PROVIDER
    provider_id: str = ""
    model_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    attempts_used: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=10)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_turns: int = Field(default=1, ge=1, le=50)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    deadline_at: datetime | None = None
    error_class: ErrorClass = ErrorClass.NONE
    error_message: str | None = None
    result_summary: str | None = None
    attempts: list[AttemptResult] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def touch(self) -> None:
        self.updated_at = utcnow()


class CreateTaskRequest(BaseModel):
    """API request body.  ``provider`` defaults to ``mock`` and real providers
    are rejected unless the service was built with an injected adapter."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=100_000)
    provider: str = DEFAULT_PROVIDER
    max_attempts: int = Field(default=1, ge=1, le=10)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_turns: int = Field(default=1, ge=1, le=50)

    @field_validator("provider")
    @classmethod
    def _normalise_provider(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("provider must not be empty")
        return cleaned


class TaskCreated(BaseModel):
    """Response to task creation.  Tasks are queued, never run synchronously."""

    id: uuid.UUID
    status: TaskStatus
    provider: str
    created_at: datetime
    location: Annotated[str, Field(description="Canonical URL of the created task.")]


class TaskView(BaseModel):
    """Full task representation returned by GET and list endpoints."""

    id: uuid.UUID
    prompt: str
    provider: str
    provider_id: str
    model_id: str
    status: TaskStatus
    attempts_used: int
    max_attempts: int
    timeout_seconds: float
    max_turns: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_class: ErrorClass
    error_message: str | None = None
    result_summary: str | None = None
    attempts: list[AttemptResult] = Field(default_factory=list)

    @classmethod
    def from_task(cls, task: Task) -> "TaskView":
        return cls(**task.model_dump())


class TaskList(BaseModel):
    items: list[TaskView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ProviderInfo(BaseModel):
    name: str
    provider_id: str
    model_id: str
    is_real: bool
    description: str
    available: bool = Field(description="True when an adapter is wired for this name.")


class HealthResponse(BaseModel):
    status: str
    version: str
    default_provider: str
    providers: list[ProviderInfo]
    task_db: str
    checkpoint_db: str
    store_open: bool
    checkpoints_open: bool
    active_dispatches: int
    allow_real_providers: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

"""Durable SQLite task store.

The task store and the LangGraph checkpoint database are deliberately separate
files with separate lifecycles:

* ``tasks.sqlite3``  -- task records + attempt history.  Owned by
  :class:`SqliteTaskStore`, which is the router's source of truth for API reads.
* ``checkpoints.sqlite3`` -- LangGraph ``SqliteSaver`` state.  Owned by
  :class:`CheckpointManager` in ``checkpointer.py``.

Keeping them apart means a checkpoint-store migration cannot take task history
with it, and each can be closed/reopened independently.

Concurrency model: one connection per thread (SQLite connections are not safe to
share), ``WAL`` journaling, and ``BEGIN IMMEDIATE`` for writes so concurrent
dispatchers serialise instead of racing on the unique-id index.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    ACTIVE_STATUSES,
    AttemptResult,
    Task,
    TaskStatus,
    utcnow,
)

__all__ = ["SqliteTaskStore", "TaskNotFound", "DuplicateTaskId"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    prompt             TEXT NOT NULL,
    provider           TEXT NOT NULL,
    provider_id        TEXT NOT NULL DEFAULT '',
    model_id           TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL,
    attempts_used      INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL,
    timeout_seconds    REAL NOT NULL,
    max_turns          INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    deadline_at        TEXT,
    error_class        TEXT NOT NULL DEFAULT 'none',
    error_message      TEXT,
    result_summary     TEXT,
    payload            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks (status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_at);

CREATE TABLE IF NOT EXISTS attempts (
    task_id     TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt_no),
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts (task_id, attempt_no);
"""

_COLUMNS = (
    "id",
    "prompt",
    "provider",
    "provider_id",
    "model_id",
    "status",
    "attempts_used",
    "max_attempts",
    "timeout_seconds",
    "max_turns",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "deadline_at",
    "error_class",
    "error_message",
    "result_summary",
)


class TaskNotFound(LookupError):
    """Raised when a task id is absent from the store."""


class DuplicateTaskId(RuntimeError):
    """Raised when an insert collides with an existing task id."""


def _dt_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _text_to_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SqliteTaskStore:
    """Thread-safe, durable task store.

    ``close()`` is idempotent; every connection handed out by :meth:`connection`
    is returned to the per-thread pool on exit.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
            from .permissions import private_file
            private_file(self._path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all_conns: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._closed = False
        self._init_schema()

    # -- lifecycle ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,  # explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        with self._conn_lock:
            self._all_conns.append(conn)
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            self._conn().executescript(_SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield this thread's connection, rolling back on error."""
        if self._closed:
            raise RuntimeError("task store is closed")
        conn = self._conn()
        try:
            yield conn
        except Exception:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            raise

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction."""
        with self._write_lock:
            with self.connection() as conn:
                nested = conn.in_transaction
                marker = f"nested_{uuid.uuid4().hex}"
                conn.execute(f"SAVEPOINT {marker}" if nested else "BEGIN IMMEDIATE")
                try:
                    yield conn
                except Exception:
                    if nested:
                        conn.execute(f"ROLLBACK TO {marker}")
                        conn.execute(f"RELEASE {marker}")
                    elif conn.in_transaction:
                        conn.rollback()
                    raise
                else:
                    if nested:
                        conn.execute(f"RELEASE {marker}")
                    else:
                        conn.commit()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._conn_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
        self._local = threading.local()

    def __enter__(self) -> "SqliteTaskStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    def create(self, task: Task) -> Task:
        """Insert a new task.  Raises :class:`DuplicateTaskId` on id collision."""
        payload = json.dumps(task.model_dump(mode="json"), sort_keys=True)
        try:
            with self._write_txn() as conn:
                conn.execute(
                    f"INSERT INTO tasks ({','.join(_COLUMNS)}, payload) "
                    f"VALUES ({','.join('?' * len(_COLUMNS))}, ?)",
                    self._row_values(task) + (payload,),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateTaskId(f"task id {task.id} already exists") from exc
        return task

    def save(self, task: Task) -> Task:
        """Persist the current state of ``task`` and its attempt history."""
        task.touch()
        payload = json.dumps(task.model_dump(mode="json"), sort_keys=True)
        with self._write_txn() as conn:
            conn.execute(
                f"UPDATE tasks SET {','.join(f'{c}=?' for c in _COLUMNS)}, payload=? "
                f"WHERE id=?",
                self._row_values(task) + (payload, str(task.id)),
            )
            for attempt in task.attempts:
                conn.execute(
                    "INSERT INTO attempts (task_id, attempt_no, payload, created_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(task_id, attempt_no) DO UPDATE SET "
                    "payload=excluded.payload",
                    (
                        str(task.id),
                        attempt.attempt,
                        json.dumps(attempt.model_dump(mode="json"), sort_keys=True),
                        _dt_to_text(utcnow()),
                    ),
                )
        return task

    def delete(self, task_id: uuid.UUID) -> None:
        with self._write_txn() as conn:
            conn.execute("DELETE FROM attempts WHERE task_id=?", (str(task_id),))
            conn.execute("DELETE FROM tasks WHERE id=?", (str(task_id),))

    def transition(self, task: Task, allowed: set[TaskStatus] | frozenset[TaskStatus]) -> bool:
        """Compare and save under the same SQLite write transaction, across processes."""
        with self._write_txn():
            if self.get(task.id).status not in allowed:
                return False
            self.save(task)
        return True

    @staticmethod
    def _row_values(task: Task) -> tuple[Any, ...]:
        return (
            str(task.id),
            task.prompt,
            task.provider,
            task.provider_id,
            task.model_id,
            task.status.value,
            task.attempts_used,
            task.max_attempts,
            task.timeout_seconds,
            task.max_turns,
            _dt_to_text(task.created_at),
            _dt_to_text(task.updated_at),
            _dt_to_text(task.started_at),
            _dt_to_text(task.finished_at),
            _dt_to_text(task.deadline_at),
            task.error_class.value,
            task.error_message,
            task.result_summary,
        )

    # -- reads --------------------------------------------------------------

    def get(self, task_id: uuid.UUID | str) -> Task:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM tasks WHERE id=?", (str(task_id),)
            ).fetchone()
        if row is None:
            raise TaskNotFound(f"task {task_id} not found")
        return Task.model_validate(json.loads(row["payload"]))

    def get_attempts(self, task_id: uuid.UUID | str) -> list[AttemptResult]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM attempts WHERE task_id=? ORDER BY attempt_no",
                (str(task_id),),
            ).fetchall()
        return [AttemptResult.model_validate(json.loads(r["payload"])) for r in rows]

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        """Return ``(tasks, total)`` ordered newest-first."""
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self.connection() as conn:
            if status is None:
                total = int(
                    conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
                )
                rows = conn.execute(
                    "SELECT payload FROM tasks ORDER BY created_at DESC, id DESC "
                    "LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM tasks WHERE status=?", (status.value,)
                    ).fetchone()["n"]
                )
                rows = conn.execute(
                    "SELECT payload FROM tasks WHERE status=? "
                    "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    (status.value, limit, offset),
                ).fetchall()
        return [Task.model_validate(json.loads(r["payload"])) for r in rows], total

    def count(self, status: TaskStatus | None = None) -> int:
        with self.connection() as conn:
            if status is None:
                return int(
                    conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
                )
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE status=?", (status.value,)
                ).fetchone()["n"]
            )

    def find_active(self) -> list[Task]:
        """Tasks still in a non-terminal, non-interrupted state (queued/running)."""
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT payload FROM tasks WHERE status IN ({placeholders})",
                tuple(s.value for s in ACTIVE_STATUSES),
            ).fetchall()
        return [Task.model_validate(json.loads(r["payload"])) for r in rows]

    def find_expired(self, *, now: datetime | None = None) -> list[Task]:
        """Active tasks whose wall-clock deadline has already elapsed."""
        now = now or utcnow()
        return [t for t in self.find_active() if self.deadline_elapsed(t, now=now)]

    @staticmethod
    def deadline_elapsed(task: Task, *, now: datetime | None = None) -> bool:
        if task.deadline_at is None:
            return False
        return (now or utcnow()) >= task.deadline_at

    @staticmethod
    def remaining_seconds(task: Task, *, now: datetime | None = None) -> float | None:
        if task.deadline_at is None:
            return None
        return (task.deadline_at - (now or utcnow())).total_seconds()

    def interrupt_stale(self, *, grace: timedelta = timedelta(seconds=0)) -> list[Task]:
        """Mark leftover queued/running tasks as ``interrupted``.

        Called on startup recovery.  No provider is invoked: a task that was
        mid-flight when the process died is *not* silently retried against a real
        provider.  ``grace`` lets tasks whose deadline is still comfortably in the
        future be re-queued by an explicit caller instead.
        """
        now = utcnow()
        recovered: list[Task] = []
        for task in self.find_active():
            deadline = task.deadline_at
            within_grace = deadline is None or now < deadline
            if within_grace:
                task.status = TaskStatus.INTERRUPTED
                task.finished_at = now
                task.error_class = (
                    task.error_class if task.attempts_used else task.error_class
                )
                task.error_message = (
                    "process restarted while task was in flight; "
                    "marked interrupted without re-dispatch"
                )
            else:
                task.status = TaskStatus.FAILED
                task.finished_at = now
                task.error_class = task.error_class
                task.error_message = (
                    "process restarted while task was in flight; "
                    "wall-clock deadline elapsed"
                )
            self.save(task)
            recovered.append(task)
        return recovered

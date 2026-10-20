"""Task service: API-facing orchestration, dispatch, and recovery.

Phase 1 invariants enforced here:

* **The API never reaches a real provider.**  The provider map is built from
  *injected* :class:`ProtocolProvider` objects.  Only ``mock`` is present by
  default; a request naming a real provider is rejected with 400 unless the
  embedding process explicitly constructed a ``HermesSubprocessProvider`` and
  passed it in *and* set ``allow_real_providers``.
* **No duplicate dispatch within a process.**  ``_in_flight`` is keyed by task
  id, guarded by a lock, so two concurrent requests for the same task cannot
  start two graph runs.
* **Crash recovery does not silently invoke a real provider.**  Tasks found
  queued/running at startup are marked terminal by ``recover_orphaned_tasks``.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any, Iterable

from .checkpointer import CheckpointManager
from .config import Settings, get_settings
from .graph import run_task_graph
from .models import (
    ACTIVE_STATUSES,
    AttemptResult,
    CreateTaskRequest,
    ErrorClass,
    Task,
    TaskList,
    TaskStatus,
    TaskView,
    utcnow,
)
from .providers.base import ProtocolProvider
from .providers.mock import MockProvider
from .registry import DEFAULT_PROVIDER, get_spec, is_real_provider, list_specs
from .store import SqliteTaskStore, TaskNotFound

__all__ = ["TaskService", "ProviderNotAvailable", "TaskAlreadyDispatched"]

logger = logging.getLogger("hermes_router.service")


class ProviderNotAvailable(ValueError):
    """Requested provider is not wired into this service instance."""


class TaskAlreadyDispatched(RuntimeError):
    """A run for this task id is already in flight in this process."""


class TaskService:
    """Owns the task store, checkpointer, provider map, and worker pool."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers: Iterable[ProtocolProvider] | None = None,
        store: SqliteTaskStore | None = None,
        checkpointer: CheckpointManager | None = None,
        max_workers: int = 4,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self._lock = threading.RLock()
        self._in_flight: set[str] = set()
        self._shutdown = threading.Event()

        self.store = store or SqliteTaskStore(self.settings.task_db_path)
        self.checkpointer = checkpointer or CheckpointManager(
            self.settings.checkpoint_db_path
        )

        provider_list = list(providers) if providers is not None else [MockProvider()]
        self._providers: dict[str, ProtocolProvider] = {p.name: p for p in provider_list}
        #: Providers that came from an explicit injection rather than the default.
        self._injected = providers is not None

        self._executor = ThreadPoolExecutorLite(max_workers=max_workers)
        self._recovered: list[str] = []

    # -- providers ----------------------------------------------------------

    @property
    def providers(self) -> dict[str, ProtocolProvider]:
        return dict(self._providers)

    def provider_available(self, name: str) -> bool:
        try:
            spec = get_spec(name)
        except KeyError:
            return False
        if not spec.is_real:
            return spec.name in self._providers
        return bool(spec.name in self._providers and self.settings.allow_real_providers)

    def describe_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "provider_id": spec.provider_id,
                "model_id": spec.model_id,
                "is_real": spec.is_real,
                "description": spec.description,
                "available": self.provider_available(spec.name),
            }
            for spec in list_specs()
        ]

    def _resolve_provider(self, name: str) -> ProtocolProvider:
        try:
            spec = get_spec(name)
        except KeyError as exc:
            raise ProviderNotAvailable(str(exc)) from exc
        if not self.provider_available(name):
            raise ProviderNotAvailable(
                f"provider {name!r} is not available in this process; "
                "real providers require an explicitly injected adapter "
                "and allow_real_providers=True"
            )
        provider = self._providers[spec.name]
        if not spec.is_real:
            return provider
        # Bind the verified ids onto the injected real adapter at dispatch time.
        configure = getattr(provider, "configure", None)
        if callable(configure):
            configure(provider_id=spec.provider_id, model_id=spec.model_id)
        return provider

    # -- API operations -----------------------------------------------------

    def create_task(self, request: CreateTaskRequest) -> Task:
        """Validate, persist as ``queued``, and schedule exactly one dispatch."""
        provider = self._resolve_provider(request.provider)
        spec = get_spec(request.provider)
        now = utcnow()
        task = Task(
            prompt=request.prompt,
            provider=spec.name,
            provider_id=spec.provider_id,
            model_id=spec.model_id,
            status=TaskStatus.QUEUED,
            max_attempts=request.max_attempts,
            timeout_seconds=request.timeout_seconds,
            max_turns=request.max_turns,
            created_at=now,
            updated_at=now,
            # Wall-clock deadline spans every attempt, not each attempt.
            deadline_at=now + timedelta(seconds=request.timeout_seconds),
        )
        self.store.create(task)
        self.dispatch(task.id)
        return task

    def get_task(self, task_id: Any) -> Task:
        return self.store.get(task_id)

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> TaskList:
        items, total = self.store.list(status=status, limit=limit, offset=offset)
        return TaskList(
            items=[TaskView.from_task(t) for t in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    # -- dispatch -----------------------------------------------------------

    @property
    def active_dispatches(self) -> int:
        with self._lock:
            return len(self._in_flight)

    @property
    def recovered_task_ids(self) -> list[str]:
        return list(self._recovered)

    def is_in_flight(self, task_id: Any) -> bool:
        with self._lock:
            return str(task_id) in self._in_flight

    def dispatch(self, task_id: Any) -> bool:
        """Schedule a background run.  Returns False if one is already in flight."""
        key = str(task_id)
        with self._lock:
            if self._shutdown.is_set():
                return False
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
        try:
            self._executor.submit(self._run_task, key)
        except RuntimeError:  # executor already shut down
            with self._lock:
                self._in_flight.discard(key)
            return False
        return True

    def _run_task(self, task_id: str) -> None:
        try:
            self._execute(task_id)
        except Exception:  # pragma: no cover - defensive; keeps the pool alive
            logger.exception("unhandled error executing task %s", task_id)
        finally:
            with self._lock:
                self._in_flight.discard(task_id)

    def _execute(self, task_id: str) -> None:
        try:
            task = self.store.get(task_id)
        except TaskNotFound:
            return
        if task.status is not TaskStatus.QUEUED:
            return
        provider = self._resolve_provider(task.provider)
        task.status = TaskStatus.RUNNING
        task.started_at = utcnow()
        if not self.store.transition(task, {TaskStatus.QUEUED}):
            return
        cancelled = lambda: self._shutdown.is_set() or self.store.get(task.id).status is TaskStatus.CANCELLED
        try:
            final = run_task_graph(provider, task_id=str(task.id), prompt=task.prompt,
                max_attempts=task.max_attempts, timeout_seconds=task.timeout_seconds,
                max_turns=task.max_turns,
                deadline_at=task.deadline_at.isoformat() if task.deadline_at else None,
                checkpointer=self.checkpointer.saver, cancelled=cancelled)
            task.attempts = [AttemptResult.model_validate(x) for x in final.get("history", [])]
            task.attempts_used = int(final.get("attempts_used", len(task.attempts)))
            task.status = TaskStatus(final.get("final_status", "failed"))
            task.error_class = ErrorClass(final.get("final_error_class", "none"))
            task.error_message = final.get("final_error_message")
            task.result_summary = final.get("summary")
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error_class = ErrorClass.ADAPTER_ERROR
            task.error_message = f"{type(exc).__name__}: execution failed"
        task.finished_at = utcnow()
        if not self.store.transition(task, {TaskStatus.RUNNING}):
            # Preserve cancellation while still retaining the completed attempt evidence.
            fresh = self.store.get(task.id)
            if fresh.status is TaskStatus.CANCELLED:
                fresh.attempts = task.attempts
                fresh.attempts_used = task.attempts_used
                self.store.transition(fresh, {TaskStatus.CANCELLED})

    def cancel_task(self, task_id: Any) -> Task:
        task = self.store.get(task_id)
        if task.status not in ACTIVE_STATUSES:
            return task
        task.status = TaskStatus.CANCELLED
        task.error_class = ErrorClass.CANCELLED
        task.error_message = "cancelled by client"
        task.finished_at = utcnow()
        self.store.transition(task, ACTIVE_STATUSES)
        provider = self._providers.get(task.provider)
        if callable(cancel := getattr(provider, "cancel", None)):
            cancel(str(task.id))
        return self.store.get(task_id)

    # -- recovery -----------------------------------------------------------

    def recover_orphaned_tasks(self) -> list[Task]:
        """Resolve tasks left queued/running by a previous process.

        No provider is invoked here.  In-flight work becomes ``interrupted`` (or
        ``failed`` when its wall-clock deadline had already elapsed), so an
        interrupted real-provider run is never silently retried for free.
        """
        recovered = self.store.interrupt_stale()
        self._recovered = [str(t.id) for t in recovered]
        if recovered:
            logger.info(
                "recovered %d orphaned task(s) without dispatch: %s",
                len(recovered),
                ", ".join(str(t.id) for t in recovered),
            )
        return recovered

    # -- lifecycle ----------------------------------------------------------

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        """Block until no dispatch is in flight.  Returns False on timeout."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._in_flight:
                    return True
            time.sleep(0.01)
        with self._lock:
            return not self._in_flight

    def close(self) -> None:
        """Shut down the worker pool, then close SQLite resources.

        Order matters: the pool drains first so no thread is mid-write when the
        store and checkpointer connections close.
        """
        if self.store.is_closed:
            return
        with self._lock:
            self._shutdown.set()
        for task in self.store.find_active():
            self.cancel_task(task.id)
        if not self._executor.shutdown(wait=True, timeout=self.settings.worker_shutdown_timeout):
            raise RuntimeError("workers still running; SQLite resources remain open")
        self.checkpointer.close()
        self.store.close()

    def __enter__(self) -> "TaskService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ThreadPoolExecutorLite:
    """Minimal fixed thread pool.

    LangGraph's sync ``invoke`` runs on the calling thread, so Phase 1 only needs
    a small pool with a bounded queue.  Implemented locally rather than via
    ``concurrent.futures`` so shutdown-with-timeout semantics are explicit and the
    module keeps a single obvious place where worker threads are created.
    """

    def __init__(self, *, max_workers: int = 4) -> None:
        self._queue: list[_Job] = []
        self._cond = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._running = 0
        self._shutdown = False
        self._max_workers = max(1, max_workers)
        for i in range(self._max_workers):
            t = threading.Thread(
                target=self._worker, name=f"hermes-router-worker-{i}", daemon=True
            )
            t.start()
            self._threads.append(t)

    def submit(self, fn, *args: Any) -> None:
        job = _Job(fn=fn, args=args)
        with self._cond:
            if self._shutdown:
                raise RuntimeError("cannot submit to a shut down pool")
            self._queue.append(job)
            self._cond.notify()

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._shutdown:
                    self._cond.wait()
                if self._shutdown and not self._queue:
                    return
                job = self._queue.pop(0)
            try:
                job.fn(*job.args)
            except Exception:  # pragma: no cover - guarded in _run_task
                pass

    def shutdown(self, *, wait: bool = True, timeout: float | None = None) -> bool:
        import time

        with self._cond:
            self._shutdown = True
            self._cond.notify_all()
        if not wait:
            return False
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
        return not any(t.is_alive() for t in self._threads)


class _Job:
    __slots__ = ("fn", "args")

    def __init__(self, fn, args: tuple[Any, ...]) -> None:
        self.fn = fn
        self.args = args

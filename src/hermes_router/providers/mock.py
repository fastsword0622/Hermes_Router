"""In-process mock provider.

This is the *only* provider the Phase 1 API can dispatch to by default.  It
never spawns a process, never touches the network, and never reads Hermes
configuration.  Tests inject scripted behaviour through :meth:`MockProvider.set_script`
so failure, timeout, and truncation paths are exercisable without a subprocess.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from ..models import AttemptResult, ErrorClass, utcnow
from ..redaction import redact_text

__all__ = ["MockProvider"]

#: Marker the mock prints in its stdout; tests assert on it.
MOCK_MARKER = "HERMES_ROUTER_MOCK_OK"


class MockProvider:
    """Deterministic provider used for all default API traffic."""

    name = "mock"
    provider_id = "mock"
    model_id = "mock-model"

    def __init__(self, *, sleep_seconds: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._script: Sequence[dict] | None = None
        self._default_sleep = sleep_seconds
        #: Every prompt this provider has seen, for assertions in tests.
        self._events: dict[str, threading.Event] = {}
        self.calls: list[tuple[str, int]] = []

    def cancel(self, thread_id: str) -> None:
        with self._lock:
            self._events.setdefault(thread_id, threading.Event()).set()

    # -- test hooks ---------------------------------------------------------

    def set_script(self, script: Sequence[dict] | None) -> None:
        """Install per-attempt scripted outcomes.

        Each entry may contain ``ok``, ``exit_code``, ``stdout``, ``stderr``,
        ``error_class``, ``error_message``, ``truncate_bytes`` and ``sleep``.
        Entries are consumed in order; the last entry repeats.
        """
        with self._lock:
            self._script = list(script) if script is not None else None

    # -- ProtocolProvider ----------------------------------------------------

    def run(
        self,
        *,
        prompt: str,
        attempt: int,
        timeout_seconds: float,
        max_turns: int,
        thread_id: str,
    ) -> AttemptResult:
        with self._lock:
            self.calls.append((thread_id, attempt))
            script = self._script
        started = utcnow()
        clock = time.monotonic()

        step: dict = {}
        if script:
            step = script[min(attempt, len(script)) - 1]

        sleep = float(step.get("sleep", self._default_sleep))
        with self._lock:
            event = self._events.setdefault(thread_id, threading.Event())
        was_cancelled = event.wait(min(sleep, timeout_seconds)) if sleep > 0 else event.is_set()
        with self._lock:
            self._events.pop(thread_id, None)
        if was_cancelled:
            return AttemptResult(attempt=attempt, provider=self.name, provider_id=self.provider_id,
                model_id=self.model_id, ok=False, error_class=ErrorClass.CANCELLED,
                error_message="cancelled by client", duration_seconds=time.monotonic()-clock)

        # Simulate the parent's hard wall-clock deadline.
        if sleep >= timeout_seconds:
            return AttemptResult(
                attempt=attempt,
                provider=self.name,
                provider_id=self.provider_id,
                model_id=self.model_id,
                ok=False,
                exit_code=None,
                stdout="",
                stderr="",
                error_class=ErrorClass.TIMEOUT,
                error_message=f"mock exceeded timeout of {timeout_seconds}s",
                duration_seconds=round(time.monotonic() - clock, 6),
                started_at=started,
                finished_at=utcnow(),
            )

        truncate = int(step.get("truncate_bytes", 0))
        stdout = step.get("stdout")
        if stdout is None:
            stdout = f"{MOCK_MARKER} provider=mock attempt={attempt} max_turns={max_turns}"
        stderr = step.get("stderr", "")
        if truncate > 0:
            stdout = (stdout + "X" * (truncate + 16))[:truncate]
            stderr = (stderr + "Y" * (truncate + 16))[:truncate]

        exit_code = int(step.get("exit_code", 0 if step.get("ok", True) else 1))
        ok = bool(step.get("ok", exit_code == 0))
        error_class = ErrorClass(step.get("error_class", "none"))
        if not ok and error_class is ErrorClass.NONE:
            error_class = ErrorClass.EXIT_CODE

        return AttemptResult(
            attempt=attempt,
            provider=self.name,
            provider_id=self.provider_id,
            model_id=self.model_id,
            ok=ok,
            exit_code=exit_code,
            stdout=redact_text(stdout),
            stderr=redact_text(stderr),
            error_class=error_class,
            error_message=step.get("error_message"),
            duration_seconds=round(time.monotonic() - clock, 6),
            truncated=truncate > 0,
            started_at=started,
            finished_at=utcnow(),
        )


ScriptFactory = Callable[[], Sequence[dict]]

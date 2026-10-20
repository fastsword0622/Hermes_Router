"""Real Hermes CLI subprocess adapter.

Security posture (all of these are structural, not advisory):

* **Prompt never enters a command line.**  The prompt is written to the child's
  stdin and read via ``--query-file -``.  There is no string interpolation of
  prompt text into argv, so prompt content cannot become a shell token.
* **``shell=False`` always.**  No shell is spawned.
* **Fresh process group.**  The child is started in a new session
  (``start_new_session=True``); on parent timeout we signal the *negative* pid,
  i.e. the whole group, so orphaned grandchildren cannot outlive the task.
* **Only Phase 0 verified flags are used.**  Exactly the flag set recorded in
  ``docs/environment-audit.md``.  No ``--yolo``, no top-level ``-z``.
* **Bounded capture.**  stdout/stderr are read through capped buffers and the
  reader threads are joined with a deadline, so a chatty child cannot exhaust
  memory or wedge the reader.
* **No configuration mutation.**  This module never writes ``config.yaml``,
  ``.env`` or ``auth.json``, and never invokes any Hermes subcommand that
  persists provider settings.  ``--provider``/``--model`` here are per-call
  overrides.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import IO, Any

from ..models import AttemptResult, ErrorClass, utcnow
from ..redaction import redact_text
from .base import MAX_CAPTURE_BYTES, MAX_INPUT_BYTES

__all__ = ["HermesSubprocessProvider", "build_argv"]

#: Read-loop grace period after the process exits, before we give up joining.
_READER_JOIN_GRACE = 0.3


def build_argv(
    *,
    executable: str,
    provider_id: str,
    model_id: str,
    max_turns: int,
    run_budget_seconds: int,
    toolsets: str | None = None,
) -> list[str]:
    """Build the exact, verified Hermes argv.

    Split out as a module function so tests can assert the flag set directly
    without spawning anything.
    """
    argv = [
        executable,
        "chat",
        "--query-file",
        "-",  # read the query from stdin
        "--oneshot",  # answer once and exit
        "-Q",  # quiet, no banner
        "--provider",
        provider_id,
        "--model",
        model_id,
        "--max-turns",
        str(max_turns),
        "--run-budget",
        str(max(1, run_budget_seconds)),
        "--ignore-rules",  # do not auto-inject AGENTS.md/SOUL.md/rules
    ]
    if toolsets is not None:
        argv.extend(["--toolsets", toolsets])
    return argv


class _CappedReader(threading.Thread):
    """Drain a pipe into a bounded buffer, discarding the overflow."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._buf = bytearray()
        self._overflow = False

    def run(self) -> None:  # pragma: no cover - exercised via subprocess tests
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    break
                room = self._limit - len(self._buf)
                if room <= 0:
                    self._overflow = True
                    continue  # keep draining so the child never blocks on write
                if len(chunk) > room:
                    self._buf.extend(chunk[:room])
                    self._overflow = True
                else:
                    self._buf.extend(chunk)
        except (ValueError, OSError):
            # Pipe closed underneath us during teardown; nothing more to read.
            pass
        finally:
            try:
                self._stream.close()
            except (ValueError, OSError):
                pass

    @property
    def text(self) -> str:
        return self._buf.decode("utf-8", errors="replace")

    @property
    def overflowed(self) -> bool:
        return self._overflow


def _kill_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the child's whole process group, ignoring already-dead children."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    targets: list[Any] = []
    if pgid is not None:
        targets.append(-pgid)  # negative pid == the process group
    targets.append(proc.pid)
    for target in targets:
        try:
            os.kill(target, sig)
        except (ProcessLookupError, PermissionError, OSError):
            continue


def _members(pgid: int) -> list[int]:
    """Inspect live members only; macOS may return EPERM for zombie-only groups."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="],
                            capture_output=True, text=True, timeout=0.5, check=True)
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.split()) == 3 and int(parts[1]) == pgid
            and not parts[2].startswith("Z")]


def _terminate_group(proc: subprocess.Popen[bytes], grace: float = 0.1) -> str | None:
    """Bounded cleanup with live-member verification; never hide surviving processes."""
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            members = _members(proc.pid)
            if not members:
                break
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                # Only signal pids still in the group we created, never arbitrary pids.
                for pid in members:
                    try:
                        if os.getpgid(pid) == proc.pid:
                            os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        remaining = _members(proc.pid)
        if remaining:
            return f"process cleanup left live group members: {remaining}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"process cleanup could not be verified: {type(exc).__name__}"
    try:
        proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        return "process leader did not terminate"
    return None


class HermesSubprocessProvider:
    """Adapter that runs ``hermes chat`` as a supervised subprocess.

    This class is **never constructed by the API**.  It is reachable only when a
    caller explicitly injects it (see ``service.TaskService``), which is the
    Phase 1 opt-in gate for real providers.
    """

    is_real = True

    def __init__(
        self,
        name: str,
        *,
        executable: str = "hermes",
        run_budget_margin: float = 5.0,
        extra_env: dict[str, str] | None = None,
        cwd: str | None = None,
        toolsets: str | None = None,
        inherit_env: bool = True,
    ) -> None:
        self.name = name
        self.executable = executable
        #: The child's internal budget sits below the parent's hard deadline so
        #: the child gets a chance to exit cleanly before we kill the group.
        self.run_budget_margin = run_budget_margin
        self._extra_env = dict(extra_env or {})
        self._cwd = cwd
        self._toolsets = toolsets
        self._inherit_env = inherit_env
        self.on_spawn = None
        self.provider_id = ""
        self.model_id = ""
        self._events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()

    def cancel(self, thread_id: str) -> None:
        with self._events_lock:
            self._events.setdefault(thread_id, threading.Event()).set()

    def _redact(self, value: str) -> str:
        secrets = [v for k, v in self._extra_env.items()
                   if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL", "PASSWD"))]
        return redact_text(value, extra_values=secrets)

    def configure(self, *, provider_id: str, model_id: str) -> None:
        """Bind the verified provider/model ids for this logical provider name."""
        self.provider_id = provider_id
        self.model_id = model_id

    def run(
        self,
        *,
        prompt: str,
        attempt: int,
        timeout_seconds: float,
        max_turns: int,
        thread_id: str,
    ) -> AttemptResult:
        from ..registry import get_spec

        provider_id = self.provider_id or get_spec(self.name).provider_id
        model_id = self.model_id or get_spec(self.name).model_id

        payload = prompt.encode("utf-8")

        run_budget = max(1, int(timeout_seconds - self.run_budget_margin))
        argv = build_argv(
            executable=self.executable,
            provider_id=provider_id,
            model_id=model_id,
            max_turns=max_turns,
            run_budget_seconds=run_budget,
            toolsets=self._toolsets,
        )

        env = {**(os.environ if self._inherit_env else {}), **self._extra_env}
        started = utcnow()
        clock = time.monotonic()
        if len(payload) > MAX_INPUT_BYTES:
            return self._failure(attempt=attempt, provider_id=provider_id, model_id=model_id,
                error_class=ErrorClass.INPUT_TOO_LARGE, message="input exceeds byte limit; request was not truncated or sent",
                clock=clock, started=started)
        with self._events_lock:
            event = self._events.setdefault(thread_id, threading.Event())
        if event.is_set():
            with self._events_lock:
                self._events.pop(thread_id, None)
            return self._failure(attempt=attempt, provider_id=provider_id, model_id=model_id,
                error_class=ErrorClass.CANCELLED, message="cancelled before process start", clock=clock, started=started)

        try:
            proc = subprocess.Popen(  # noqa: S603 - shell=False, fixed argv shape
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,  # fresh process group
                env=env,
                cwd=self._cwd,
            )
        except (OSError, ValueError) as exc:
            with self._events_lock:
                self._events.pop(thread_id, None)
            return self._failure(
                attempt=attempt,
                provider_id=provider_id,
                model_id=model_id,
                error_class=ErrorClass.SPAWN_FAILED,
                message=f"{type(exc).__name__}: {exc}",
                clock=clock,
                started=started,
                stderr="",
            )

        stdout_reader = _CappedReader(proc.stdout, MAX_CAPTURE_BYTES)  # type: ignore[arg-type]
        stderr_reader = _CappedReader(proc.stderr, MAX_CAPTURE_BYTES)  # type: ignore[arg-type]
        stdout_reader.start()
        stderr_reader.start()
        if self.on_spawn is not None:
            try:
                self.on_spawn(proc.pid)
            except Exception:
                _terminate_group(proc)
                with self._events_lock:
                    self._events.pop(thread_id, None)
                return self._failure(attempt=attempt, provider_id=provider_id, model_id=model_id,
                    error_class=ErrorClass.ADAPTER_ERROR, message="could not persist process identity", clock=clock, started=started)

        timed_out = False
        write_error: str | None = None
        def write_input() -> None:
            nonlocal write_error
            try:
                if proc.stdin is not None:
                    proc.stdin.write(payload)
                    proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                write_error = f"{type(exc).__name__} writing stdin: {exc}"
            finally:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()

        while proc.poll() is None and not event.is_set():
            remaining = timeout_seconds - (time.monotonic() - clock)
            if remaining <= 0:
                timed_out = True
                break
            event.wait(min(remaining, 0.02))
        cleanup_error = _terminate_group(proc)
        exit_code = proc.poll()
        with self._events_lock:
            self._events.pop(thread_id, None)
        writer.join(timeout=0.5)

        stdout_reader.join(timeout=_READER_JOIN_GRACE)
        stderr_reader.join(timeout=_READER_JOIN_GRACE)
        truncated = stdout_reader.overflowed or stderr_reader.overflowed
        stdout = stdout_reader.text
        stderr = stderr_reader.text
        if write_error:
            stderr = f"{stderr}\n{write_error}".strip()

        if cleanup_error or event.is_set():
            return self._failure(attempt=attempt, provider_id=provider_id, model_id=model_id,
                error_class=ErrorClass.CLEANUP_FAILED if cleanup_error else ErrorClass.CANCELLED,
                message=cleanup_error or "cancelled by client", clock=clock, started=started,
                stdout=stdout, stderr=stderr, exit_code=exit_code, truncated=truncated)

        if timed_out:
            return self._failure(
                attempt=attempt,
                provider_id=provider_id,
                model_id=model_id,
                error_class=ErrorClass.TIMEOUT,
                message=(
                    f"wall-clock deadline of {timeout_seconds}s exceeded; "
                    "process group terminated"
                ),
                clock=clock,
                started=started,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                truncated=truncated,
            )

        if exit_code is None:
            return self._failure(
                attempt=attempt,
                provider_id=provider_id,
                model_id=model_id,
                error_class=ErrorClass.ADAPTER_ERROR,
                message="process did not report an exit status",
                clock=clock,
                started=started,
                stdout=stdout,
                stderr=stderr,
                truncated=truncated,
            )

        ok = exit_code == 0
        return AttemptResult(
            attempt=attempt,
            provider=self.name,
            provider_id=provider_id,
            model_id=model_id,
            ok=ok,
            exit_code=exit_code,
            stdout=self._redact(stdout),
            stderr=self._redact(stderr),
            error_class=ErrorClass.NONE if ok else ErrorClass.EXIT_CODE,
            error_message=None if ok else f"hermes exited with code {exit_code}",
            duration_seconds=round(time.monotonic() - clock, 6),
            truncated=truncated,
            started_at=started,
            finished_at=utcnow(),
        )

    def _failure(
        self,
        *,
        attempt: int,
        provider_id: str,
        model_id: str,
        error_class: ErrorClass,
        message: str,
        clock: float,
        started: Any,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        truncated: bool = False,
    ) -> AttemptResult:
        return AttemptResult(
            attempt=attempt,
            provider=self.name,
            provider_id=provider_id,
            model_id=model_id,
            ok=False,
            exit_code=exit_code,
            stdout=self._redact(stdout),
            stderr=self._redact(stderr),
            error_class=error_class,
            error_message=self._redact(message),
            duration_seconds=round(time.monotonic() - clock, 6),
            truncated=truncated,
            started_at=started,
            finished_at=utcnow(),
        )

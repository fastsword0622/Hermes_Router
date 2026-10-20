"""Bounded subprocess capture shared by Git and approved verification commands."""
import os
import selectors
import subprocess
import time
from pathlib import Path
from .providers.hermes import _terminate_group
from .redaction import redact_text


def process_identity(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=,pgid="],
        capture_output=True, text=True, timeout=1, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    fields = result.stdout.strip().rsplit(None, 1)
    if result.returncode or len(fields) != 2:
        return None
    return {"pid": pid, "started": fields[0], "pgid": int(fields[1])}


def stop_saved_process(identity):
    """Only stop the same process/session recorded before the crash, never a reused PID."""
    from .providers.hermes import _members
    import signal
    if not identity:
        return "not_recorded"
    current = process_identity(identity["pid"])
    if current != identity:
        return "original_process_gone"
    if current["pgid"] != current["pid"]:
        return "identity_unverified"
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            members = _members(current["pgid"])
            if not members:
                return "terminated"
            try:
                os.killpg(current["pgid"], sig)
            except ProcessLookupError:
                return "terminated"
            except PermissionError:
                for pid in members:
                    try:
                        if os.getpgid(pid) == current["pgid"]:
                            os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            time.sleep(0.05)
        return "terminated" if not _members(current["pgid"]) else "cleanup_unverified"
    except (OSError, subprocess.SubprocessError):
        return "cleanup_unverified"


def run_command(argv: list[str], cwd: Path, timeout: float = 10, *, env=None, cancelled=lambda: False, limit=131072, on_spawn=None):
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return dict(exit_code=None, stdout="", stderr=type(exc).__name__, duration_seconds=0,
                    timed_out=False, cancelled=False, truncated=False, cleanup_error=None)
    if on_spawn is not None:
        try:
            on_spawn(proc.pid)
        except Exception:
            _terminate_group(proc)
            proc.stdout.close()
            proc.stderr.close()
            raise
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    timed_out = False
    was_cancelled = False
    with selectors.DefaultSelector() as selector:
        for name in buffers:
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            if cancelled():
                was_cancelled = True
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
            for key, _ in selector.select(0.02):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buf = buffers[key.data]
                room = max(0, limit - len(buf))
                truncated |= len(data) > room
                buf.extend(data[:room])
            if proc.poll() is not None and not selector.get_map():
                break
    cleanup = _terminate_group(proc)
    for stream in (proc.stdout, proc.stderr):
        stream.close()
    return dict(exit_code=proc.poll(), stdout=redact_text(buffers["stdout"].decode(errors="replace")),
        stderr=redact_text(buffers["stderr"].decode(errors="replace")), duration_seconds=time.monotonic()-started,
        timed_out=timed_out, cancelled=was_cancelled, truncated=truncated, cleanup_error=cleanup)

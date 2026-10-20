"""Hermes subprocess adapter tested against a fake executable.

A shell script stands in for ``hermes`` so the adapter's process supervision,
argv construction, exit-code classification, timeout group-kill, and output
bounding can all be verified without any real provider call.
"""

from __future__ import annotations

import os
import signal
import stat
import textwrap
import time
from pathlib import Path

import pytest

from hermes_router.models import ErrorClass
from hermes_router.providers.base import MAX_CAPTURE_BYTES
from hermes_router.providers.hermes import HermesSubprocessProvider, build_argv


def write_fake(tmp_path: Path, body: str, name: str = "fake_hermes") -> str:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def provider_for(executable: str, name: str = "minimax") -> HermesSubprocessProvider:
    return HermesSubprocessProvider(name, executable=executable, run_budget_margin=0.0)


# -- argv construction -------------------------------------------------------


def test_argv_uses_only_verified_flags() -> None:
    argv = build_argv(
        executable="hermes",
        provider_id="minimax-cn",
        model_id="MiniMax-M3",
        max_turns=3,
        run_budget_seconds=30,
    )
    assert argv == [
        "hermes",
        "chat",
        "--query-file",
        "-",
        "--oneshot",
        "-Q",
        "--provider",
        "minimax-cn",
        "--model",
        "MiniMax-M3",
        "--max-turns",
        "3",
        "--run-budget",
        "30",
        "--ignore-rules",
    ]


def test_argv_never_contains_yolo_or_top_level_z() -> None:
    argv = build_argv(
        executable="hermes",
        provider_id="zai",
        model_id="glm-5.3",
        max_turns=1,
        run_budget_seconds=10,
    )
    assert "--yolo" not in argv
    assert "-z" not in argv
    assert argv[0] == "hermes"
    assert argv[1] == "chat"
    # First flag position must be a subcommand flag, never bare -z.
    assert all(a != "-z" for a in argv)


def test_run_budget_is_clamped_to_at_least_one_second() -> None:
    argv = build_argv(
        executable="hermes",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        max_turns=1,
        run_budget_seconds=0,
    )
    assert argv[argv.index("--run-budget") + 1] == "1"


# -- successful execution ----------------------------------------------------


def test_stdin_is_read_and_prompt_is_not_in_argv(tmp_path: Path) -> None:
    exe = write_fake(
        tmp_path,
        """
        echo "ARGS=$*"
        echo "---"
        cat
        """,
    )
    prompt = "hello world; rm -rf / ; $(whoami)"
    result = provider_for(exe).run(
        prompt=prompt, attempt=1, timeout_seconds=10.0, max_turns=2, thread_id="t1"
    )
    assert result.ok is True
    assert result.exit_code == 0
    # The prompt arrived on stdin.
    assert prompt in result.stdout
    # ...and is not part of the argument vector at all.
    assert "rm -rf" not in result.stdout.split("---")[0]
    assert "ARGS=chat --query-file" in result.stdout


def test_zero_exit_is_success_but_not_acceptance(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, 'echo "done"; exit 0')
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t1"
    )
    assert result.exit_code == 0
    assert result.ok is True
    assert result.error_class is ErrorClass.NONE
    # The adapter never asserts acceptance; that decision belongs to the graph.
    assert not hasattr(result, "accepted")


def test_provider_ids_are_bound_from_registry(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, 'cat > /dev/null; exit 0')
    for name, expected in (("minimax", "MiniMax-M3"), ("glm", "glm-5.3"), ("deepseek", "deepseek-v4-pro")):
        result = provider_for(exe, name).run(
            prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
        )
        assert result.model_id == expected


def test_configure_overrides_bound_ids(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, "exit 0")
    p = provider_for(exe)
    p.configure(provider_id="custom", model_id="custom-model")
    result = p.run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    assert result.provider_id == "custom"
    assert result.model_id == "custom-model"


# -- failure classification --------------------------------------------------


@pytest.mark.parametrize("code", [1, 2, 42, 127])
def test_nonzero_exit_is_failure(tmp_path: Path, code: int) -> None:
    exe = write_fake(tmp_path, f"exit {code}")
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    assert result.ok is False
    assert result.exit_code == code
    assert result.error_class is ErrorClass.EXIT_CODE
    assert str(code) in (result.error_message or "")


def test_stderr_is_captured(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, 'echo "boom" >&2; exit 1')
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    assert "boom" in result.stderr
    assert result.ok is False


def test_spawn_failure_is_classified(tmp_path: Path) -> None:
    result = provider_for(str(tmp_path / "does-not-exist")).run(
        prompt="x", attempt=1, timeout_seconds=5.0, max_turns=1, thread_id="t"
    )
    assert result.ok is False
    assert result.error_class is ErrorClass.SPAWN_FAILED
    assert "FileNotFoundError" in (result.error_message or "")


# -- timeout and process-group kill -----------------------------------------


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    """A grandchild spawned by the fake hermes must also die on parent timeout."""
    marker = tmp_path / "grandchild-alive"
    exe = write_fake(
        tmp_path,
        f"""
        # Child of the script: survives unless the whole group is signalled.
        ( sleep 30; echo alive > {marker} ) &
        sleep 30
        """,
    )
    started = time.monotonic()
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=1.0, max_turns=1, thread_id="t"
    )
    elapsed = time.monotonic() - started
    assert result.ok is False
    assert result.error_class is ErrorClass.TIMEOUT
    assert "process group terminated" in (result.error_message or "")
    assert elapsed < 10.0  # did not wait out the 30s sleep

    # The grandchild must not be able to write the marker afterwards.
    time.sleep(2.0)
    assert not marker.exists(), "grandchild survived the process-group kill"


def test_child_is_in_a_fresh_process_group(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, 'echo "$$"; ps -o pgid= -p $$ | tr -d " "; ps -o pgid= -p $PPID | tr -d " "')
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    child_pid = lines[0].strip()
    child_pgid = lines[1].strip()
    parent_pgid = lines[2].strip()
    assert child_pgid != parent_pgid
    assert child_pgid == child_pid
    assert parent_pgid == str(os.getpgid(os.getpid()))


def test_stdin_write_failure_is_reported_but_not_fatal(tmp_path: Path) -> None:
    # The fake exits immediately without reading stdin -> broken pipe on write.
    exe = write_fake(tmp_path, "exit 0")
    result = provider_for(exe).run(
        prompt="x" * 100_000,
        attempt=1,
        timeout_seconds=10.0,
        max_turns=1,
        thread_id="t",
    )
    assert result.ok is True
    assert result.exit_code == 0


# -- output bounding and redaction -------------------------------------------


def test_large_output_is_bounded_and_flagged(tmp_path: Path) -> None:
    exe = write_fake(
        tmp_path,
        """
        i=0
        while [ $i -lt 4000 ]; do
          echo "0123456789012345678901234567890123456789"
          i=$((i+1))
        done
        """,
    )
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=20.0, max_turns=1, thread_id="t"
    )
    assert result.truncated is True
    assert len(result.stdout.encode()) <= MAX_CAPTURE_BYTES


def test_stderr_also_bounded(tmp_path: Path) -> None:
    exe = write_fake(
        tmp_path,
        """
        i=0
        while [ $i -lt 4000 ]; do
          echo "0123456789012345678901234567890123456789" >&2
          i=$((i+1))
        done
        exit 1
        """,
    )
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=20.0, max_turns=1, thread_id="t"
    )
    assert result.truncated is True
    assert len(result.stderr.encode()) <= MAX_CAPTURE_BYTES


def test_oversized_prompt_is_rejected_without_silent_truncation(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, "wc -c")
    result = provider_for(exe).run(
        prompt="A" * 500_000,
        attempt=1,
        timeout_seconds=15.0,
        max_turns=1,
        thread_id="t",
    )
    assert result.ok is False
    assert result.error_class is ErrorClass.INPUT_TOO_LARGE
    assert result.exit_code is None


def test_output_is_redacted(tmp_path: Path) -> None:
    exe = write_fake(
        tmp_path,
        'echo "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"; echo "Bearer abcdef1234567890xyz" >&2',
    )
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    assert "sk-abcdefghijklmnopqrstuvwx" not in result.stdout
    assert "abcdef1234567890xyz" not in result.stderr
    assert "[REDACTED]" in result.stdout


def test_duration_and_timestamps_recorded(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, "exit 0")
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    assert result.duration_seconds >= 0.0
    assert result.started_at is not None
    assert result.finished_at is not None
    assert result.finished_at >= result.started_at


def test_signal_death_is_reported_as_failure(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, 'kill -TERM $$')
    result = provider_for(exe).run(
        prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t"
    )
    assert result.ok is False
    # sh reports 128+signal for a signalled child.
    assert result.exit_code is not None and result.exit_code != 0
    assert result.error_class is ErrorClass.EXIT_CODE


def test_kill_group_tolerates_already_dead_process(tmp_path: Path) -> None:
    exe = write_fake(tmp_path, "exit 0")
    p = provider_for(exe)
    result = p.run(prompt="x", attempt=1, timeout_seconds=10.0, max_turns=1, thread_id="t")
    assert result.ok is True
    # Killing a finished process group must not raise.
    from hermes_router.providers.hermes import _kill_process_group

    class _FakeProc:
        pid = 999_999  # pid that certainly does not exist
        returncode = 0

        def poll(self):
            return 0

    _kill_process_group(_FakeProc(), signal.SIGKILL)  # type: ignore[arg-type]

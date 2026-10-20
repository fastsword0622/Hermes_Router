"""Independent execution boundaries: use real subprocesses and durable state."""
import os
import sys
import time
from datetime import timedelta

from hermes_router.checkpointer import CheckpointManager
from hermes_router.config import Settings
from hermes_router.graph import run_task_graph
from hermes_router.models import CreateTaskRequest, TaskStatus, utcnow
from hermes_router.providers.hermes import HermesSubprocessProvider
from hermes_router.providers.mock import MockProvider
from hermes_router.service import TaskService


def fake(tmp_path, code):
    executable = tmp_path / "fake-hermes"
    executable.write_text(f"#!{sys.executable}\n" + code)
    executable.chmod(0o700)
    return str(executable)


def test_timeout_includes_blocked_stdin(tmp_path):
    exe = fake(tmp_path, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    result = HermesSubprocessProvider("minimax", executable=exe).run(
        prompt="x" * 250_000, attempt=1, timeout_seconds=0.15,
        max_turns=1, thread_id="blocked-stdin",
    )
    assert result.error_class.value == "timeout"
    assert time.monotonic() - started < 2


def test_timeout_kills_descendant_ignoring_term(tmp_path):
    pidfile = tmp_path / "child.pid"
    exe = fake(tmp_path, f"""import subprocess,sys,time
p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'])
open({str(pidfile)!r},'w').write(str(p.pid))
time.sleep(30)
""")
    result = HermesSubprocessProvider("minimax", executable=exe).run(
        prompt="x", attempt=1, timeout_seconds=2.0, max_turns=1, thread_id="descendant",
    )
    assert result.error_class.value == "timeout"
    import subprocess
    pid = pidfile.read_text()
    state = subprocess.run(["ps", "-o", "stat=", "-p", pid], capture_output=True, text=True).stdout.strip()
    assert not state or state.startswith("Z")


def test_all_attempts_are_persisted(settings):
    provider = MockProvider()
    provider.set_script([{"ok": False}, {"ok": False}, {"ok": True}])
    with TaskService(settings, providers=[provider]) as service:
        task = service.create_task(CreateTaskRequest(prompt="retry", max_attempts=3))
        assert service.wait_for_idle(5)
        saved = service.get_task(task.id)
        assert saved.status == TaskStatus.SUCCEEDED
        assert [a.attempt for a in saved.attempts] == [1, 2, 3]
        assert len(service.store.get_attempts(task.id)) == 3


def test_real_zero_exit_requires_verification(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", allow_real_providers=True)
    provider = HermesSubprocessProvider("minimax", executable=fake(tmp_path, "print('done')\n"))
    with TaskService(settings, providers=[provider]) as service:
        task = service.create_task(CreateTaskRequest(prompt="do something", provider="minimax"))
        assert service.wait_for_idle(5)
        assert service.get_task(task.id).status == TaskStatus.AWAITING_VERIFICATION


def test_checkpoint_does_not_persist_prompt_secret(settings):
    with TaskService(settings) as service:
        task = service.create_task(CreateTaskRequest(prompt="API_KEY=pretend-secret-value-12345"))
        assert service.wait_for_idle(5)
        assert "pretend-secret" not in service.get_task(task.id).prompt
        checkpoint = service.checkpointer.saver.get(service.checkpointer.thread_config(str(task.id)))
        assert "pretend-secret" not in str(checkpoint["channel_values"])


def test_graph_deadline_clamps_provider_budget(tmp_path):
    class Recorder(MockProvider):
        def run(self, **kwargs):
            self.budget = kwargs["timeout_seconds"]
            time.sleep(0.08)
            return super().run(**kwargs)
    provider = Recorder()
    with CheckpointManager(tmp_path / "checkpoints.db") as cp:
        state = run_task_graph(provider, task_id="budget", prompt="x", max_attempts=5,
            timeout_seconds=60, max_turns=1,
            deadline_at=(utcnow() + timedelta(seconds=0.06)).isoformat(), checkpointer=cp.saver)
    assert provider.budget < 0.1
    assert state["final_status"] == "failed"
    assert state["final_error_class"] == "deadline_exceeded"

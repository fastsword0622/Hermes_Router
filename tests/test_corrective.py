import sys
import time
from hermes_router.config import Settings
from hermes_router.models import CreateTaskRequest, Task, TaskStatus
from hermes_router.providers.hermes import HermesSubprocessProvider
from hermes_router.providers.mock import MockProvider
from hermes_router.service import TaskService
from hermes_router.store import SqliteTaskStore


def executable(tmp_path, code):
    p = tmp_path / "hermes-fake"
    p.write_text(f"#!{sys.executable}\n" + code)
    p.chmod(0o700)
    return str(p)


def test_child_only_credential_is_redacted(tmp_path):
    secret = "opaque-private-value-without-vendor-prefix"
    p = HermesSubprocessProvider("minimax", extra_env={"CUSTOM_API_KEY": secret},
        executable=executable(tmp_path, "import os\nprint(os.environ['CUSTOM_API_KEY'])\n"))
    result = p.run(prompt="x", attempt=1, timeout_seconds=5, max_turns=1, thread_id="redact")
    assert result.exit_code == 0
    assert secret not in result.model_dump_json()


def test_cancel_stops_real_process_and_preserves_state(tmp_path):
    ready = tmp_path / "ready"
    exe = executable(tmp_path, f"import time\nopen({str(ready)!r},'w').write('ready')\ntime.sleep(30)\n")
    settings = Settings(data_dir=tmp_path / "data", allow_real_providers=True)
    with TaskService(settings, providers=[HermesSubprocessProvider("minimax", executable=exe)]) as svc:
        task = svc.create_task(CreateTaskRequest(prompt="x", provider="minimax", timeout_seconds=30))
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert ready.exists()
        started = time.monotonic()
        svc.cancel_task(task.id)
        assert svc.wait_for_idle(2)
        assert time.monotonic() - started < 2
        assert svc.get_task(task.id).status is TaskStatus.CANCELLED


def test_cancel_cannot_be_overwritten_by_stale_writer(tmp_path):
    with SqliteTaskStore(tmp_path / "tasks.db") as a, SqliteTaskStore(tmp_path / "tasks.db") as b:
        original = a.create(Task(prompt="x"))
        stale = b.get(original.id)
        original.status = TaskStatus.CANCELLED
        a.save(original)
        stale.status = TaskStatus.RUNNING
        assert not b.transition(stale, {TaskStatus.QUEUED})
        assert a.get(original.id).status is TaskStatus.CANCELLED


def test_shutdown_cancels_worker_before_closing_sqlite(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", worker_shutdown_timeout=2)
    svc = TaskService(settings, providers=[MockProvider(sleep_seconds=10)])
    task = svc.create_task(CreateTaskRequest(prompt="x", timeout_seconds=20))
    time.sleep(0.05)
    svc.close()
    assert svc.store.is_closed
    with SqliteTaskStore(settings.task_db_path) as store:
        assert store.get(task.id).status is TaskStatus.CANCELLED


def test_short_child_only_credential_is_not_persisted(tmp_path):
    provider = HermesSubprocessProvider("minimax", extra_env={"PRIVATE_CREDENTIAL": "abc"},
        executable=executable(tmp_path, "import os\nprint(os.environ['PRIVATE_CREDENTIAL'])\n"))
    result = provider.run(prompt="x", attempt=1, timeout_seconds=5, max_turns=1, thread_id="short")
    assert result.exit_code == 0
    assert "abc" not in result.stdout


def test_cancel_before_registration_never_starts_process(tmp_path):
    marker = tmp_path / "started"
    provider = HermesSubprocessProvider("minimax", executable=executable(tmp_path, f"open({str(marker)!r},'w').write('started')\n"))
    provider.cancel("early")
    result = provider.run(prompt="x", attempt=1, timeout_seconds=5, max_turns=1, thread_id="early")
    assert result.error_class.value == "cancelled"
    assert not marker.exists()

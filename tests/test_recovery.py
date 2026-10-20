import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import pytest
from hermes_router.development import DevelopmentService, DevelopmentStore, JobConflict
from hermes_router.providers.hermes import _members
from tests.test_workspace import approved, checked_spec, repository, wait
from tests.spec_factory import minimal_spec


def test_single_owner_and_release(tmp_path):
    first = DevelopmentService(tmp_path / "data")
    with pytest.raises(JobConflict, match="another Router"):
        DevelopmentService(tmp_path / "data")
    first.close()
    second = DevelopmentService(tmp_path / "data")
    second.close()


def test_actual_sigkill_recovers_without_automatic_replay_and_resumes(tmp_path):
    repo = repository(tmp_path)
    data = tmp_path / "data"
    service = DevelopmentService(data, (repo,))
    code = "import os,time; from pathlib import Path; p=Path(os.environ['HOME'])/'once'; old=p.exists(); p.write_text('ready'); time.sleep(0 if old else 30); from app import value; assert value==1"
    spec = minimal_spec(verification_commands=[{"id": "C1", "argv": ["python3", "-c", code], "timeout_seconds": 60}])
    job_id = approved(service, repo, spec, [{"edits": [{"path": "app.py", "content": "value = 1\n"}]}], timeout_seconds=120)
    service.close()
    script = tmp_path / "crash-router.py"
    script.write_text(f"from pathlib import Path\nimport time\nfrom hermes_router.development import DevelopmentService\ns=DevelopmentService(Path({str(data)!r}),(Path({str(repo)!r}),))\ns.runner.start({job_id!r})\ntime.sleep(90)\n")
    env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()), PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    observer = DevelopmentStore(data / "development.sqlite3")
    try:
        until = time.monotonic() + 15
        marker = data / "execution" / job_id / "once"
        while time.monotonic() < until:
            current = observer.get(job_id)
            if marker.exists() and current.get("process") and current.get("pending_attempt") == 1:
                break
            if child.poll() is not None:
                pytest.fail("child Router exited before SIGKILL: " + child.stderr.read().decode()[:2000])
            time.sleep(0.02)
        else:
            pytest.fail("verification did not reach a persisted in-flight state")
        identity = current["process"]
        deadline = current["deadline_at"]
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=5) == -signal.SIGKILL
        observer.close()
        recovered = DevelopmentService(data, (repo,))
        try:
            job = recovered.store.get(job_id)
            assert job["status"] == "interrupted"
            assert job["attempts"][0]["error"] == "interrupted"
            assert job["pending_attempt"] is None
            assert not recovered.runner.futures
            assert not _members(identity["pgid"])
            assert Path(job["worktree"], "app.py").read_text() == "value = 1\n"
            assert (repo / "app.py").read_text() == "value = 0\n"
            with pytest.raises(JobConflict, match="acknowledge"):
                recovered.runner.resume(job_id, "human", False)
            ready = recovered.runner.resume(job_id, "human", True)
            assert ready["deadline_at"] == deadline
            recovered.runner.start(job_id)
            final = wait(recovered, job_id)
            assert final["status"] == "succeeded", final["report"]
            assert [a["number"] for a in final["attempts"]] == [1, 2]
            assert final["attempts"][1]["verified"] is True
        finally:
            recovered.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stderr.close()


@pytest.mark.parametrize("which", ["attempts", "deadline"])
def test_resume_does_not_rearm_limits(tmp_path, which):
    from datetime import timedelta
    from hermes_router.models import utcnow
    service = DevelopmentService(tmp_path / "data")
    job = service.store.create("x", "/repo", max_attempts=1)
    def interrupt(j):
        j.update(status="interrupted")
        if which == "attempts":
            j["attempts"] = [{"number": 1, "error": "interrupted"}]
        else:
            j["deadline_at"] = (utcnow()-timedelta(seconds=1)).isoformat()
    service.store.mutate(job["id"], interrupt)
    with pytest.raises(JobConflict, match="budget|deadline"):
        service.runner.resume(job["id"], "human", True)
    service.close()

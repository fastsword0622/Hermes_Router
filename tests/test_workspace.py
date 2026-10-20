import subprocess
import sys
import time
from pathlib import Path
import pytest
from hermes_router.development import DevelopmentService
from hermes_router.workspace import ChangeProposal, Workspace, WorkspaceError, verify_commands
from tests.spec_factory import minimal_spec


def repository(tmp_path, with_app=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("test repository\n")
    if with_app:
        (repo / "app.py").write_text("value = 0\n")
    for args in (["init", "-b", "main"], ["add", "."],
        ["-c", "user.name=Router Test", "-c", "user.email=router@example.invalid", "commit", "-m", "baseline"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def approved(service, repo, spec, plan=(), **options):
    job = service.store.create("Implement the approved function", str(repo), mock_plan=list(plan), **options)
    service.store.set_spec(job["id"], spec)
    service.store.approve(job["id"], 1, spec.digest(), "test-human")
    return job["id"]


def wait(service, job_id):
    service.runner.futures[job_id].result(timeout=25)
    return service.store.get(job_id)


def checked_spec(**changes):
    return minimal_spec(verification_commands=[{"id": "C1", "argv": ["python3", "-c", "from app import value; assert value == 1"]}], **changes)


def test_worktree_success_diff_and_original_untouched(tmp_path):
    repo = repository(tmp_path)
    service = DevelopmentService(tmp_path / "data", (repo,))
    job_id = approved(service, repo, checked_spec(), [{"edits": [{"path": "app.py", "content": "value = 1\n"}]}])
    service.runner.start(job_id)
    job = wait(service, job_id)
    assert job["status"] == "succeeded", job.get("report")
    assert (repo / "app.py").read_text() == "value = 0\n"
    assert Path(job["worktree"], "app.py").read_text() == "value = 1\n"
    assert job["report"]["changes"]["files"] == ["app.py"]
    assert "+value = 1" in job["report"]["changes"]["diff"]
    assert job["report"]["verification"][0]["sandboxed"] is True
    service.close()


def test_repeated_real_test_failure_switches_to_glm(tmp_path):
    repo = repository(tmp_path)
    service = DevelopmentService(tmp_path / "data", (repo,))
    bad = {"edits": [{"path": "app.py", "content": "value = 0\n"}]}
    good = {"edits": [{"path": "app.py", "content": "value = 1\n"}]}
    job_id = approved(service, repo, checked_spec(), [bad, bad, good])
    service.runner.start(job_id)
    job = wait(service, job_id)
    assert job["status"] == "succeeded", job["report"]
    assert [a["provider"] for a in job["attempts"]] == ["minimax", "minimax", "glm"]
    assert [a["verification"][0]["passed"] for a in job["attempts"]] == [False, False, True]
    service.close()


@pytest.mark.parametrize("forbidden", ["check:C2", "do not change dependencies"])
def test_additional_failing_command_prevents_success(tmp_path, forbidden):
    repo = repository(tmp_path)
    service = DevelopmentService(tmp_path / "data", (repo,))
    try:
        spec = minimal_spec(forbidden_changes=[forbidden], verification_commands=[
            {"id": "C1", "argv": ["python3", "-c", "from app import value; assert value == 1"]},
            {"id": "C2", "argv": ["python3", "-c", "assert False, 'policy check failed'"]}])
        job_id = approved(service, repo, spec, [{"edits": [{"path": "app.py", "content": "value = 1\n"}]}], max_attempts=2)
        service.runner.start(job_id)
        job = wait(service, job_id)
        assert job["status"] == "needs_user_action"
        assert job["reason"] == "max_attempts"
        assert len(job["attempts"]) == 2
        assert all(not a["verified"] and a["test_failures"] == ["C2"] for a in job["attempts"])
        assert job["report"]["verification"][1]["exit_code"] != 0
    finally:
        service.close()


def test_forbidden_package_is_rejected_before_any_write(tmp_path):
    repo = repository(tmp_path)
    service = DevelopmentService(tmp_path / "data", (repo,))
    plan = [{"edits": [{"path": "app.py", "content": "value = 1\n"}, {"path": "forbidden.py", "content": "bad"}]}]
    job_id = approved(service, repo, checked_spec(), plan)
    service.runner.start(job_id)
    job = wait(service, job_id)
    assert job["status"] == "blocked"
    assert job["reason"] == "forbidden_change"
    assert Path(job["worktree"], "app.py").read_text() == "value = 0\n"
    assert not Path(job["worktree"], "forbidden.py").exists()
    service.close()


def test_schema_change_requires_approval_before_write(tmp_path):
    repo = repository(tmp_path)
    service = DevelopmentService(tmp_path / "data", (repo,))
    spec = minimal_spec(allowed_files=["schema.sql"])
    job_id = approved(service, repo, spec, [{"edits": [{"path": "schema.sql", "content": "CREATE TABLE sample(id INTEGER);"}]}])
    service.runner.start(job_id)
    job = wait(service, job_id)
    assert job["status"] == "awaiting_risk_approval"
    assert not job["attempts"]
    assert not Path(job["worktree"], "schema.sql").exists()
    service.runner.approve_risk(job_id, job["risk_request"]["digest"], "human")
    service.runner.start(job_id)
    job = wait(service, job_id)
    assert job["status"] == "succeeded", job["report"]
    assert Path(job["worktree"], "schema.sql").exists()
    service.close()


def test_dirty_repository_is_not_touched(tmp_path):
    repo = repository(tmp_path)
    (repo / "app.py").write_text("user dirty change\n")
    workspace = Workspace(repo, tmp_path / "trees", "job", (repo,))
    with pytest.raises(WorkspaceError, match="dirty"):
        workspace.prepare()
    assert not workspace.path.exists()
    assert (repo / "app.py").read_text() == "user dirty change\n"


def test_ignored_files_remain_in_diff_and_policy_evidence(tmp_path):
    repo = repository(tmp_path)
    (repo / ".gitignore").write_text("*.py\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Router Test", "-c", "user.email=router@example.invalid", "commit", "-m", "ignore generated files"], check=True, capture_output=True)
    workspace = Workspace(repo, tmp_path / "trees", "ignored-evidence", (repo,)).prepare()
    spec = minimal_spec(allowed_files=["generated.py"])
    workspace.apply(ChangeProposal(edits=[{"path": "generated.py", "content": "value = 1\n"}]), spec)
    evidence = workspace.collect(spec)
    assert evidence["files"] == ["generated.py"]
    assert "+value = 1" in evidence["diff"]
    (workspace.path / "blocked.py").write_text("unexpected change\n")
    assert workspace.collect(spec)["illegal_files"] == ["blocked.py"]


def test_symlink_escape_is_rejected(tmp_path):
    repo = repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("unchanged")
    workspace = Workspace(repo, tmp_path / "trees", "job", (repo,)).prepare()
    (workspace.path / "linked.py").symlink_to(outside)
    spec = minimal_spec(allowed_files=["linked.py"])
    with pytest.raises(WorkspaceError, match="escapes|symlink"):
        workspace.apply(ChangeProposal.model_validate({"edits": [{"path": "linked.py", "content": "changed"}]}), spec)
    assert outside.read_text() == "unchanged"


def test_case_variant_metadata_is_protected(tmp_path):
    repo = repository(tmp_path)
    workspace = Workspace(repo, tmp_path / "trees", "job", (repo,)).prepare()
    before = (workspace.path / ".git").read_bytes()
    with pytest.raises(WorkspaceError, match="forbidden"):
        workspace.apply(ChangeProposal.model_validate({"edits": [{"path": ".GIT", "content": "gitdir: /other"}]}),
                        minimal_spec(allowed_files=["*"]), risk_approved=True)
    assert (workspace.path / ".git").read_bytes() == before


def test_verification_sandbox_denies_outside_writes_and_network(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside"
    code = f"from pathlib import Path; Path({str(outside)!r}).write_text('bad')"
    spec = minimal_spec(verification_commands=[{"id": "C1", "argv": ["python3", "-c", code]}])
    results = verify_commands(tree, tmp_path / "scratch", spec, 5)
    assert results[0]["passed"] is False
    assert not outside.exists()
    network = minimal_spec(verification_commands=[{"id": "C1", "argv": ["python3", "-c", "import socket; socket.socket().connect(('127.0.0.1',9))"]}])
    results = verify_commands(tree, tmp_path / "scratch2", network, 5)
    assert not results[0]["passed"]
    assert "Operation not permitted" in results[0]["stderr"]


def test_verification_denies_private_reads_and_executable_siblings(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    secret = tools / "private.txt"
    secret.write_text("private-personal-content")
    command = tools / "check.sh"
    command.write_text(f"#!/bin/sh\n/bin/cat '{secret}'\n")
    command.chmod(0o700)
    spec = minimal_spec(verification_commands=[{"id": "C1", "argv": [str(command)]}])
    result = verify_commands(tree, tmp_path / "scratch-private", spec, 5)[0]
    assert not result["passed"]
    assert "private-personal-content" not in result["stdout"]
    spec = minimal_spec(verification_commands=[{"id": "C1", "argv": ["python3", "-c", f"from pathlib import Path; print(Path({str(secret)!r}).read_text())"]}])
    result = verify_commands(tree, tmp_path / "scratch-direct", spec, 5)[0]
    assert not result["passed"]
    assert "private-personal-content" not in result["stdout"]

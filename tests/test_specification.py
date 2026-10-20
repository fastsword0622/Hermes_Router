import sqlite3
import pytest
from pydantic import ValidationError
from hermes_router.development import DevelopmentStore, JobConflict
from hermes_router.specification import WorkSpec, analyze_request
from tests.spec_factory import minimal_spec


def test_natural_request_becomes_draft_with_questions():
    draft = analyze_request("로그인을 추가해 주세요")
    assert draft["draft"]["objective"] == "로그인을 추가해 주세요"
    assert {q["field"] for q in draft["questions"]} >= {"allowed_files", "acceptance_criteria", "verification_commands"}


@pytest.mark.parametrize("field", list(WorkSpec.model_fields))
def test_spec_requires_all_eight_fields(field):
    body = minimal_spec().model_dump(mode="json")
    del body[field]
    with pytest.raises(ValidationError):
        WorkSpec.model_validate(body)


def test_every_requirement_needs_verifiable_acceptance():
    with pytest.raises(ValidationError, match="each requirement"):
        minimal_spec(non_functional_requirements=[{"id": "N1", "description": "Python compatible"}, {"id": "N2", "description": "missing evidence"}])


@pytest.mark.parametrize("rule", ["hermes core", "provider settings"])
def test_unspecified_forbidden_scope_requires_precise_paths(rule):
    with pytest.raises(ValidationError, match="ambiguous forbidden change"):
        minimal_spec(forbidden_changes=[rule])


@pytest.mark.parametrize("pattern", ["/etc/passwd", "../file", ".git/config", ".GIT/config", "a/../b", "a\\b"])
def test_reject_path_escape(pattern):
    with pytest.raises(ValidationError):
        minimal_spec(allowed_files=[pattern])


def test_immutable_approved_version_and_restart(tmp_path):
    path = tmp_path / "jobs.db"
    store = DevelopmentStore(path)
    job = store.create("create function", str(tmp_path))
    assert job["status"] == "needs_clarification"
    spec = minimal_spec()
    draft = store.set_spec(job["id"], spec)
    assert draft["status"] == "awaiting_spec_approval"
    with pytest.raises(JobConflict):
        store.approve(job["id"], 1, "0" * 64, "human")
    store.approve(job["id"], 1, spec.digest(), "human")
    with pytest.raises(JobConflict):
        store.set_spec(job["id"], minimal_spec(objective="changed"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.conn.execute("UPDATE specifications SET payload='{}' WHERE job_id=?", (job["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.conn.execute("DELETE FROM specifications WHERE job_id=?", (job["id"],))
    store.close()
    store = DevelopmentStore(path)
    assert store.get(job["id"])["specification"]["body"] == spec.model_dump(mode="json")
    assert store.get(job["id"])["approved_digest"] == spec.digest()
    assert store.events(job["id"])[-1]["kind"] == "specification_approved"
    store.close()


def test_stale_approval_rejected(tmp_path):
    store = DevelopmentStore(tmp_path / "jobs.db")
    job = store.create("task", str(tmp_path))
    old = minimal_spec()
    store.set_spec(job["id"], old)
    new = minimal_spec(objective="new objective")
    store.set_spec(job["id"], new)
    with pytest.raises(JobConflict):
        store.approve(job["id"], 1, old.digest(), "human")
    assert store.approve(job["id"], 2, new.digest(), "human")["status"] == "ready"
    store.close()


def test_job_api_gates_execution(client):
    job = client.post("/v1/jobs", json={"prompt": "fix", "repository": "/repo"}).json()
    assert job["status"] == "needs_clarification"
    assert client.get(f"/v1/jobs/{job['id']}").json()["attempts"] == []
    spec = minimal_spec()
    draft = client.put(f"/v1/jobs/{job['id']}/specification", json=spec.model_dump(mode="json")).json()
    assert draft["status"] == "awaiting_spec_approval"
    approved = client.post(f"/v1/jobs/{job['id']}/approve", json={"version": 1, "digest": spec.digest(), "actor": "human"})
    assert approved.json()["status"] == "ready"
    assert client.put(f"/v1/jobs/{job['id']}/specification", json=spec.model_dump(mode="json")).status_code == 409

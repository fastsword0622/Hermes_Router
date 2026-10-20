import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_router.development import DevelopmentService, JobConflict
from hermes_router.hermes_runtime import HermesFactory
from hermes_router.models import AttemptResult
from hermes_router.routing import classify
from tests.test_workspace import approved, checked_spec, repository, wait
from tests.spec_factory import minimal_spec


def factory(tmp_path):
    credentials = tmp_path / "existing-hermes.env"
    credentials.write_text("MINIMAX_CN_API_KEY=fake-provider-key-12345\nMINIMAX_CN_BASE_URL=https://api.minimax.cn/anthropic\n")
    exe = tmp_path / "fake-hermes"
    proposal = {"edits": [{"path": "app.py", "content": "value = 1\n"}], "summary": "done", "requirement_notes": {}}
    exe.write_text(f"#!{sys.executable}\nimport os,json,sys\nassert 'ROUTER_ADMIN_TOKEN' not in os.environ\nassert 'UNRELATED_SECRET' not in os.environ\nassert '--toolsets' in sys.argv and sys.argv[-1]=='web'\nassert os.environ['MINIMAX_CN_API_KEY']=='fake-provider-key-12345'\nprint({json.dumps(json.dumps(proposal))})\n")
    exe.chmod(0o700)
    f = HermesFactory(tmp_path / "data", executable=str(exe), credentials_path=credentials,
        environ={"PATH": os.environ["PATH"], "ROUTER_ADMIN_TOKEN": "private-admin-token", "UNRELATED_SECRET": "private-value"})
    return f, credentials


def test_isolated_cli_proposal_drives_verified_worktree(tmp_path):
    repo = repository(tmp_path)
    f, credentials = factory(tmp_path)
    before = credentials.read_bytes()
    service = DevelopmentService(tmp_path / "data", (repo,), f)
    job_id = approved(service, repo, checked_spec(), mode="hermes")
    service.runner.start(job_id)
    job = wait(service, job_id)
    assert job["status"] == "succeeded", job["report"]
    assert job["report"]["simulation"] is False
    assert credentials.read_bytes() == before
    config = json.loads((tmp_path / "data/hermes-homes" / job_id / "minimax/config.yaml").read_text())
    assert config["agent"]["disabled_toolsets"] == ["web"]
    assert "fake-provider-key" not in json.dumps(config)
    assert "fake-provider-key" not in json.dumps(job)
    assert (repo / "app.py").read_text() == "value = 0\n"
    service.close()


def test_unconfigured_credential_does_not_fall_back(tmp_path):
    f = HermesFactory(tmp_path, credentials_path=tmp_path / "missing", environ={})
    with pytest.raises(ValueError, match="credential not configured"):
        f.make("glm", "job", tmp_path / "cwd")


def test_analysis_stops_for_user_approval(tmp_path):
    class FakeFactory:
        def make(self, *args):
            return SimpleNamespace(run=lambda **kw: AttemptResult(attempt=1, provider="minimax", provider_id="minimax-cn",
                model_id="MiniMax-M3", ok=True, exit_code=0, stdout=json.dumps({"draft": minimal_spec().model_dump(mode="json"), "questions": []})))
    service = DevelopmentService(tmp_path / "data", provider_factory=FakeFactory())
    job = service.store.create("Add a function", "/repo", "hermes")
    result = service.analyze(job["id"])
    assert result["status"] == "awaiting_spec_approval"
    assert result["approved_digest"] is None
    assert result["attempts"] == []
    service.store.approve(job["id"], 1, result["specification"]["digest"], "human")
    with pytest.raises(JobConflict):
        service.analyze(job["id"])
    service.close()


def test_generated_code_literals_are_not_transport_errors():
    assert classify({"exit_code": 0, "stdout": '{"edits":[],"summary":"HTTP 429 and ConnectionError handling"}'}) == "none"


def test_skill_contains_supported_approval_and_reporting_flow():
    text = Path("skills/dev-router/SKILL.md").read_text()
    for command in ("create", "status", "failures", "history", "cancel", "approve", "report"):
        assert "dev-router " + command in text
    assert "Never approve your own draft" in text
    assert "--request-file" in text

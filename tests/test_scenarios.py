import pytest
from hermes_router.development import DevelopmentService
from tests.test_workspace import approved, checked_spec, repository, wait

BAD = {"edits": [{"path": "app.py", "content": "value = 0\n"}]}
GOOD = {"edits": [{"path": "app.py", "content": "value = 1\n"}]}


@pytest.mark.parametrize("scenario,plan,providers,status,reason", [
    ("glm_failure_diagnosis", [BAD, BAD, BAD, BAD, {}], ["minimax", "minimax", "glm", "glm", "deepseek"], "needs_user_action", "deepseek_diagnosis"),
    ("rate_limit", [{"exit_code": 1, "stderr": "HTTP 429"}, GOOD], ["minimax", "glm"], "succeeded", "acceptance_verified"),
    ("network", [{"exit_code": 1, "stderr": "ConnectionError"}]*3+[GOOD], ["minimax", "minimax", "minimax", "glm"], "succeeded", "acceptance_verified"),
    ("attempt_limit", [BAD, BAD], ["minimax", "minimax"], "needs_user_action", "max_attempts"),
])
def test_end_to_end_scenarios(tmp_path, scenario, plan, providers, status, reason):
    repo = repository(tmp_path)
    service = DevelopmentService(tmp_path / "data", (repo,))
    try:
        job_id = approved(service, repo, checked_spec(), plan, max_attempts=2 if scenario == "attempt_limit" else 10)
        service.runner.start(job_id)
        job = wait(service, job_id)
        assert job["status"] == status, job["report"]
        assert job["reason"] == reason
        assert [a["provider"] for a in job["attempts"]] == providers
        assert (repo / "app.py").read_text() == "value = 0\n"
        if scenario in {"attempt_limit", "glm_failure_diagnosis"}:
            assert job["attempts"][0]["verification"][0]["passed"] is False
            assert job["attempts"][0]["verification"][0]["stderr"]
        if scenario == "glm_failure_diagnosis":
            assert job["attempts"][-1]["handoff"]["diagnostic_only"] is True
    finally:
        service.close()


def test_missing_required_file_switches_immediately(tmp_path):
    repo = repository(tmp_path, with_app=False)
    service = DevelopmentService(tmp_path / "data", (repo,))
    criteria = [{"id": "A1", "description": "required function artifact", "requirement_ids": ["F1", "N1"],
                 "verification_command_ids": ["C1"], "required_files": ["app.py"]}]
    try:
        job_id = approved(service, repo, checked_spec(acceptance_criteria=criteria), [{}, GOOD])
        service.runner.start(job_id)
        job = wait(service, job_id)
        assert job["status"] == "succeeded", job["report"]
        assert [a["provider"] for a in job["attempts"]] == ["minimax", "glm"]
        assert set(job["attempts"][0]["missing_requirements"]) == {"F1", "N1"}
    finally:
        service.close()

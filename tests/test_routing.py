from datetime import timedelta
from hermes_router.checkpointer import CheckpointManager
from hermes_router.models import utcnow
from hermes_router.routing import build_router
from tests.spec_factory import minimal_spec


def route(tmp_path, script, max_attempts=10, seconds=10):
    calls = []
    outcomes = []
    def execute(provider, package, remaining, diagnostic):
        calls.append((provider, package, diagnostic))
        step = script[min(len(calls), len(script)) - 1]
        outcomes.append(step)
        return step.get("result", {"exit_code": 0})
    def verify(provider, result, spec):
        return outcomes[-1].get("verification", {"verified": True})
    spec = minimal_spec()
    with CheckpointManager(tmp_path / "cp.db") as cp:
        app = build_router(execute, verify).compile(checkpointer=cp.saver)
        result = app.invoke(dict(job_id="job", specification=spec.model_dump(mode="json"),
            spec_digest=spec.digest(), deadline_at=(utcnow()+timedelta(seconds=seconds)).isoformat(),
            max_attempts=max_attempts, provider_index=0, attempts=[], switches=[]),
            {"configurable": {"thread_id": "job"}, "recursion_limit": 100})
    return result, calls


def test_minimax_success(tmp_path):
    state, calls = route(tmp_path, [{}])
    assert state["status"] == "succeeded"
    assert [p for p, _, _ in calls] == ["minimax"]


def test_missing_requirement_switches_to_glm_with_handoff(tmp_path):
    state, calls = route(tmp_path, [{"verification": {"verified": False, "missing_requirements": ["F1"]}}, {}])
    assert [p for p, _, _ in calls] == ["minimax", "glm"]
    assert state["switches"][0]["reason"] == "requirements_missing"
    assert calls[1][1].history[0]["missing_requirements"] == ["F1"]
    assert calls[1][1].spec_digest == minimal_spec().digest()


def test_same_test_twice_then_glm(tmp_path):
    failure = {"verification": {"verified": False, "test_failures": ["C1"]}}
    state, calls = route(tmp_path, [failure, failure, {}])
    assert [p for p, _, _ in calls] == ["minimax", "minimax", "glm"]
    assert state["status"] == "succeeded"


def test_glm_failure_leads_to_diagnostic_only(tmp_path):
    failure = {"verification": {"verified": False, "missing_requirements": ["F1"]}}
    state, calls = route(tmp_path, [failure, failure, {}])
    assert [p for p, _, _ in calls] == ["minimax", "glm", "deepseek"]
    assert calls[-1][2] is True
    assert calls[-1][1].diagnostic_only is True
    assert state["status"] == "needs_user_action"
    assert state["reason"] == "deepseek_diagnosis"


def test_429_switches_without_retry(tmp_path):
    state, calls = route(tmp_path, [{"result": {"exit_code": 1, "stderr": "HTTP 429"}}, {}])
    assert [p for p, _, _ in calls] == ["minimax", "glm"]
    assert state["switches"][0]["reason"] == "rate_limit"


def test_network_is_retried_only_twice(tmp_path):
    error = {"result": {"exit_code": 1, "stderr": "ConnectionError"}}
    state, calls = route(tmp_path, [error, error, error, {}])
    assert [p for p, _, _ in calls] == ["minimax", "minimax", "minimax", "glm"]
    assert state["status"] == "succeeded"


def test_forbidden_change_stops_without_handoff(tmp_path):
    state, calls = route(tmp_path, [{"verification": {"illegal_change": True}}])
    assert state["status"] == "blocked"
    assert len(calls) == 1


def test_risky_change_pauses(tmp_path):
    state, calls = route(tmp_path, [{"verification": {"risk_required": True}}])
    assert state["status"] == "awaiting_risk_approval"
    assert len(calls) == 1


def test_attempt_limit_reports_user_action(tmp_path):
    state, calls = route(tmp_path, [{"verification": {"verified": False, "test_failures": ["C1"]}}], max_attempts=2)
    assert len(calls) == 2
    assert state["reason"] == "max_attempts"


def test_expired_deadline_does_not_call_any_provider(tmp_path):
    state, calls = route(tmp_path, [{}], seconds=-1)
    assert not calls
    assert state["reason"] == "deadline_exceeded"

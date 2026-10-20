"""Provider escalation driven by independent verification, never model claims."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable, TypedDict
from langgraph.graph import END, START, StateGraph
from .models import utcnow
from .redaction import redact_text
from .specification import FrozenModel, WorkSpec

PROVIDERS = ("minimax", "glm", "deepseek")


class HandoffPackage(FrozenModel):
    job_id: str
    spec_digest: str
    specification: WorkSpec
    target_provider: str
    diagnostic_only: bool
    remaining_attempts: int
    remaining_seconds: float
    history: tuple[dict, ...]


class RouterState(TypedDict, total=False):
    job_id: str
    specification: dict
    spec_digest: str
    deadline_at: str
    max_attempts: int
    provider_index: int
    attempts: list[dict]
    switches: list[dict]
    network_retries: int
    repeated_failures: dict[str, int]
    status: str
    reason: str
    action: str
    last: dict
    resume_action: str


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [scrub(v) for v in value]
    return value


def classify(result: dict) -> str:
    if result.get("error_class") in ("cancelled", "timeout", "spawn_failed", "adapter_error", "output_truncated", "cleanup_failed", "input_too_large"):
        return result["error_class"]
    # Successful generated code can legitimately contain 429/ConnectionError literals.
    text = (result.get("stderr", "") + "\n" + (result.get("stdout", "") if result.get("exit_code") != 0 else "")).lower()
    if re.search(r"\b429\b|rate.?limit|quota|usage.limit", text):
        return "rate_limit"
    if re.search(r"connectionerror|connection error|connecterror|network unreachable|name resolution|connection reset|dns error", text):
        return "network"
    if re.search(r"\b50[0234]\b|temporarily unavailable|overloaded", text):
        return "recoverable_api"
    if re.search(r"\b40[13]\b|authenticationerror|invalid api key", text):
        return "authentication"
    if result.get("truncated"):
        return "output_truncated"
    return "none" if result.get("exit_code") == 0 else "execution_failed"


def build_router(
    executor: Callable[[str, HandoffPackage, float, bool], dict],
    verifier: Callable[[str, dict, WorkSpec], dict],
    *, cancelled: Callable[[], bool] = lambda: False,
    on_update: Callable[[dict], None] = lambda state: None,
    network_retry_limit: int = 2,
):
    graph = StateGraph(RouterState)

    def attempt(state):
        remaining = (datetime.fromisoformat(state["deadline_at"]) - utcnow()).total_seconds()
        if cancelled():
            return {"status": "cancelled", "reason": "cancelled", "action": "end"}
        if remaining <= 0:
            return {"status": "failed", "reason": "deadline_exceeded", "action": "end"}
        if len(state.get("attempts", [])) >= state["max_attempts"]:
            return {"status": "needs_user_action", "reason": "max_attempts", "action": "end"}
        index = state.get("provider_index", 0)
        provider = PROVIDERS[index]
        spec = WorkSpec.model_validate(state["specification"])
        if spec.digest() != state["spec_digest"]:
            return {"status": "blocked", "reason": "specification_changed", "action": "end"}
        handoff = HandoffPackage(job_id=state["job_id"], spec_digest=state["spec_digest"], specification=spec,
            target_provider=provider, diagnostic_only=index == 2,
            remaining_attempts=state["max_attempts"] - len(state.get("attempts", [])),
            remaining_seconds=remaining, history=tuple({k: a.get(k) for k in
                ("provider", "number", "error", "missing_requirements", "test_failures", "verification")}
                for a in state.get("attempts", [])[-8:]))
        on_update({**state, "status": "running", "in_flight_provider": provider,
                   "pending_attempt": len(state.get("attempts", [])) + 1})
        started = utcnow()
        try:
            result = scrub(executor(provider, handoff, remaining, index == 2))
        except Exception as exc:
            result = {"exit_code": None, "error_class": "adapter_error", "stderr": type(exc).__name__}
        error = classify(result)
        try:
            verification = scrub(verifier(provider, result, spec))
        except Exception as exc:
            verification = {"verified": False, "test_failures": ["verification_error:" + type(exc).__name__]}
        record = {"number": len(state.get("attempts", [])) + 1, "provider": provider,
            "started_at": started.isoformat(), "finished_at": utcnow().isoformat(),
            "duration_seconds": (utcnow() - started).total_seconds(),
            "handoff": handoff.model_dump(mode="json"), "result": result,
            "error": error, **verification}
        attempts = [*state.get("attempts", []), record]
        update = {"attempts": attempts, "last": record, "status": "running", "action": "decide"}
        on_update({**state, **update, "pending_attempt": None})
        return update

    def decide_impl(state):
        if state.get("action") == "end":
            return {}
        last = state["last"]
        def end(status, reason):
            return {"status": status, "reason": reason, "action": "end"}
        if cancelled() or last["error"] == "cancelled":
            return end("cancelled", "cancelled")
        if last["error"] == "cleanup_failed" or last.get("cleanup_failed"):
            return end("blocked", "cleanup_unverified")
        if last["error"] == "input_too_large":
            return end("needs_user_action", "input_too_large")
        if last.get("illegal_change"):
            return end("blocked", "forbidden_change")
        if last.get("risk_required"):
            return end("awaiting_risk_approval", "risky_change")
        if utcnow() >= datetime.fromisoformat(state["deadline_at"]):
            return end("failed", "deadline_exceeded")
        if state.get("provider_index", 0) == 2:
            return end("needs_user_action", "deepseek_diagnosis")
        if last["error"] == "none" and last.get("verified") is True:
            return end("succeeded", "acceptance_verified")
        if len(state["attempts"]) >= state["max_attempts"]:
            return end("needs_user_action", "max_attempts")
        if last["error"] in ("network", "recoverable_api"):
            retries = state.get("network_retries", 0)
            if retries < network_retry_limit:
                return {"network_retries": retries + 1, "action": "retry", "reason": last["error"]}
            return {"action": "switch", "reason": "network_retries_exhausted"}
        if last.get("missing_requirements") or last["error"] in ("rate_limit", "authentication", "execution_failed", "spawn_failed", "adapter_error", "timeout", "output_truncated"):
            return {"action": "switch", "reason": "requirements_missing" if last.get("missing_requirements") else last["error"]}
        failures = sorted(last.get("test_failures") or ["acceptance_unverified"])
        fingerprint = json.dumps(failures, sort_keys=True)
        counts = dict(state.get("repeated_failures", {}))
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        return {"repeated_failures": counts, "action": "switch" if counts[fingerprint] >= 2 else "retry",
                "reason": "repeated_test_failure" if counts[fingerprint] >= 2 else "verification_failed"}

    def decide(state):
        update = decide_impl(state)
        on_update({**state, **update})
        return update

    def switch(state):
        index = state.get("provider_index", 0)
        transition = {"from": PROVIDERS[index], "to": PROVIDERS[index + 1],
            "reason": state["reason"], "after_attempt": len(state["attempts"]), "timestamp": utcnow().isoformat()}
        switches = [*state.get("switches", []), transition]
        update = {"provider_index": index + 1, "switches": switches, "network_retries": 0, "repeated_failures": {}, "action": "retry"}
        on_update({**state, **update})
        return update

    def reverify(state):
        last = dict(state["attempts"][-1])
        if last["error"] == "none" and state.get("provider_index", 0) < 2:
            last.update(scrub(verifier(last["provider"], last["result"], WorkSpec.model_validate(state["specification"]))))
        update = {"attempts": [*state["attempts"][:-1], last], "last": last, "action": "decide"}
        on_update({**state, **update})
        return update

    graph.add_node("attempt", attempt)
    graph.add_node("decide", decide)
    graph.add_node("switch", switch)
    graph.add_node("reverify", reverify)
    graph.add_conditional_edges(START, lambda state: state.get("resume_action", "attempt"),
        {"attempt": "attempt", "reverify": "reverify", "switch": "switch"})
    graph.add_edge("reverify", "decide")
    graph.add_edge("attempt", "decide")
    graph.add_conditional_edges("decide", lambda state: state["action"],
        {"end": END, "retry": "attempt", "switch": "switch"})
    graph.add_edge("switch", "attempt")
    return graph

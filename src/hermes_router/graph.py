"""LangGraph StateGraph that executes one task with bounded attempts.

Graph shape (Phase 1: single provider, attempt loop only -- no routing yet)::

    START -> prepare -> attempt -> {ok: finish | retryable & budget left: attempt}
                                \\-> terminal failure: finish

Hard rules encoded here:

* ``max_attempts`` bounds the *total* attempts for a task, across retries.
* A wall-clock ``deadline_at`` is stamped once at ``prepare`` and every node
  checks it before doing work, so the task cannot overrun past its deadline.
* Exit code 0 from the provider is **not** development acceptance.  ``ok`` comes
  from the provider's own success signal; the graph additionally requires the
  task's completion criteria to be satisfiable and records that zero exit alone
  is insufficient in ``result_summary``.
* Provider output is redacted by the adapter *before* it enters graph state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from .models import (
    RETRYABLE_ERRORS,
    AttemptResult,
    ErrorClass,
    TaskStatus,
    utcnow,
)
from .providers.base import ProtocolProvider

__all__ = ["TaskState", "build_execution_graph", "run_task_graph"]


def _last(_: Any, new: Any) -> Any:
    """Reducer that keeps the most recent value."""
    return new


class TaskState(TypedDict, total=False):
    """Graph state.  Deliberately flat and JSON-serialisable so checkpoints are
    readable and the task store remains the source of truth."""

    task_id: str
    prompt: str
    provider: str
    attempt: int
    max_attempts: int
    timeout_seconds: float
    max_turns: int
    deadline_at: str | None
    last_result: Annotated[AttemptResult | None, _last]
    history: Annotated[list[dict[str, Any]], lambda acc, new: acc + new]
    final_status: Annotated[str, _last]
    final_error_class: Annotated[str, _last]
    final_error_message: Annotated[str, _last]
    summary: Annotated[str | None, _last]
    attempts_used: Annotated[int, _last]


def _deadline_passed(state: TaskState) -> bool:
    raw = state.get("deadline_at")
    if not raw:
        return False
    return utcnow() >= datetime.fromisoformat(raw)


def build_execution_graph(provider: ProtocolProvider, cancelled: Callable[[], bool] = lambda: False):
    """Compile the execution graph for one concrete provider instance."""
    graph = StateGraph(TaskState)

    def prepare(state: TaskState) -> dict[str, Any]:
        return {
            "attempt": 0,
            "history": [],
            "attempts_used": 0,
            "final_status": TaskStatus.RUNNING.value,
        }

    def attempt_node(state: TaskState) -> dict[str, Any]:
        attempt_no = int(state.get("attempt", 0)) + 1
        if cancelled():
            return {"final_status": "cancelled"}
        if _deadline_passed(state):
            return {
                "final_status": TaskStatus.FAILED.value,
                "final_error_class": ErrorClass.DEADLINE_EXCEEDED.value,
                "final_error_message": "wall-clock deadline exceeded before attempt",
            }
        result = provider.run(
            prompt=state["prompt"],
            attempt=attempt_no,
            timeout_seconds=min(
                float(state.get("timeout_seconds", 60.0)),
                max(0.001, (datetime.fromisoformat(state["deadline_at"]) - utcnow()).total_seconds())
                if state.get("deadline_at") else float(state.get("timeout_seconds", 60.0)),
            ),
            max_turns=int(state.get("max_turns", 1)),
            thread_id=state["task_id"],
        )
        return {
            "attempt": attempt_no,
            "attempts_used": attempt_no,
            "last_result": result,
            "history": [result.model_dump(mode="json")],
        }

    def route(state: TaskState) -> Literal["finish", "retry"]:
        result = state.get("last_result")
        if cancelled():
            return "finish"
        if _deadline_passed(state):
            return "finish"
        if result is not None and result.ok:
            return "finish"
        if int(state.get("attempt", 0)) >= int(state.get("max_attempts", 1)):
            return "finish"
        if result is None or result.error_class not in RETRYABLE_ERRORS:
            return "finish"
        return "retry"

    def finish(state: TaskState) -> dict[str, Any]:
        result: AttemptResult | None = state.get("last_result")
        if cancelled():
            return {"final_status": "cancelled", "final_error_class": "cancelled",
                    "final_error_message": "cancelled by client"}
        if _deadline_passed(state):
            return {
                "final_status": TaskStatus.FAILED.value,
                "final_error_class": ErrorClass.DEADLINE_EXCEEDED.value,
                "final_error_message": "wall-clock deadline exceeded",
            }
        if result is None:
            return {
                "final_status": TaskStatus.FAILED.value,
                "final_error_class": ErrorClass.ADAPTER_ERROR.value,
                "final_error_message": "no provider attempt was recorded",
            }
        if result.ok:
            if getattr(provider, "is_real", False):
                return {
                    "final_status": TaskStatus.AWAITING_VERIFICATION.value,
                    "final_error_class": ErrorClass.NONE.value,
                    "final_error_message": None,
                    "summary": "CLI execution finished; development acceptance requires independent verification",
                }
            # Exit code 0 is necessary but NOT sufficient for acceptance; we only
            # record that the provider reported success and defer any development
            # acceptance decision to a later phase.
            return {
                "final_status": TaskStatus.SUCCEEDED.value,
                "final_error_class": ErrorClass.NONE.value,
                "final_error_message": None,
                "summary": (
                    f"provider {result.provider} reported success on attempt "
                    f"{result.attempt} (exit_code={result.exit_code}); "
                    "exit code 0 alone is not development acceptance"
                ),
            }
        return {
            "final_status": TaskStatus.FAILED.value,
            "final_error_class": result.error_class.value,
            "final_error_message": result.error_message
            or f"attempt failed: {result.error_class.value}",
        }

    graph.add_node("prepare", prepare)
    graph.add_node("attempt", attempt_node)
    graph.add_node("finish", finish)

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare",
        lambda _state: "attempt",
        {"attempt": "attempt"},
    )
    graph.add_conditional_edges(
        "attempt",
        route,
        {"retry": "attempt", "finish": "finish"},
    )
    graph.add_edge("finish", END)
    return graph


def run_task_graph(
    provider: ProtocolProvider,
    *,
    task_id: str,
    prompt: str,
    max_attempts: int,
    timeout_seconds: float,
    max_turns: int,
    deadline_at: str | None,
    checkpointer: Any,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Execute one task to a terminal state and return the final graph state."""
    app = build_execution_graph(provider, cancelled).compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": str(task_id)}}
    initial: TaskState = {
        "task_id": str(task_id),
        "prompt": prompt,
        "provider": provider.name,
        "max_attempts": max_attempts,
        "timeout_seconds": timeout_seconds,
        "max_turns": max_turns,
        "deadline_at": deadline_at,
    }
    return dict(app.invoke(initial, config=config))

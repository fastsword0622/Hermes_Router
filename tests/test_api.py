"""HTTP API surface: create / get / list / attempts / cancel / health."""

from __future__ import annotations

import uuid

import pytest

from hermes_router.models import TaskStatus
from hermes_router.providers.mock import MOCK_MARKER


def create(client, **body):
    payload = {"prompt": "test prompt"}
    payload.update(body)
    return client.post("/v1/tasks", json=payload)


def test_create_returns_202_with_uuid_and_location(client) -> None:
    resp = create(client)
    assert resp.status_code == 202
    body = resp.json()
    uuid.UUID(body["id"])  # raises if not a UUID
    assert body["status"] in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}
    assert body["provider"] == "mock"
    assert resp.headers["Location"] == f"/v1/tasks/{body['id']}"


def test_provider_defaults_to_mock(client) -> None:
    resp = client.post("/v1/tasks", json={"prompt": "no provider given"})
    assert resp.json()["provider"] == "mock"


def test_real_provider_request_is_rejected(client) -> None:
    for name in ("minimax", "glm", "deepseek"):
        resp = create(client, provider=name)
        assert resp.status_code == 400
        assert "not available" in resp.json()["detail"]


def test_unknown_provider_is_rejected(client) -> None:
    resp = create(client, provider="totally-made-up")
    assert resp.status_code == 400
    assert "unknown provider" in resp.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"prompt": ""},
        {"prompt": "x", "max_attempts": 0},
        {"prompt": "x", "max_attempts": 99},
        {"prompt": "x", "timeout_seconds": 0},
        {"prompt": "x", "max_turns": 0},
        {"prompt": "x", "unknown_field": 1},
    ],
)
def test_validation_rejects_bad_bodies(client, body: dict) -> None:
    assert client.post("/v1/tasks", json=body).status_code == 422


def test_empty_provider_rejected(client) -> None:
    assert client.post("/v1/tasks", json={"prompt": "x", "provider": "  "}).status_code == 422


def test_get_task_roundtrip(client, service) -> None:
    task_id = create(client).json()["id"]
    assert service.wait_for_idle(10.0)
    resp = client.get(f"/v1/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == task_id
    assert body["status"] == TaskStatus.SUCCEEDED.value
    assert body["attempts_used"] == 1
    assert MOCK_MARKER in body["attempts"][0]["stdout"]


def test_get_missing_task_is_404(client) -> None:
    resp = client.get(f"/v1/tasks/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_get_malformed_id_is_422(client) -> None:
    assert client.get("/v1/tasks/not-a-uuid").status_code == 422


def test_task_ids_are_unique_across_creates(client) -> None:
    ids = {create(client).json()["id"] for _ in range(10)}
    assert len(ids) == 10


def test_list_returns_all_tasks(client, service) -> None:
    ids = {create(client).json()["id"] for _ in range(3)}
    service.wait_for_idle(10.0)
    resp = client.get("/v1/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert {i["id"] for i in body["items"]} == ids


def test_list_filters_by_status(client, service) -> None:
    ok_id = create(client).json()["id"]
    assert service.wait_for_idle(10.0)
    mock_provider = next(iter(service._providers.values()))
    mock_provider.set_script([{"ok": False, "exit_code": 3}])
    bad_id = create(client).json()["id"]
    service.wait_for_idle(10.0)

    failed = client.get("/v1/tasks", params={"status": "failed"}).json()
    assert failed["total"] == 1
    assert failed["items"][0]["id"] == bad_id

    succeeded = client.get("/v1/tasks", params={"status": "succeeded"}).json()
    assert succeeded["total"] == 1
    assert succeeded["items"][0]["id"] == ok_id


def test_list_pagination(client) -> None:
    for _ in range(5):
        create(client)
    page = client.get("/v1/tasks", params={"limit": 2, "offset": 1}).json()
    assert len(page["items"]) == 2
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert page["total"] == 5


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"status": "nonsense"}]
)
def test_list_rejects_bad_params(client, params: dict) -> None:
    assert client.get("/v1/tasks", params=params).status_code == 422


def test_attempts_endpoint(client, service) -> None:
    task_id = create(client, max_attempts=1).json()["id"]
    service.wait_for_idle(10.0)
    body = client.get(f"/v1/tasks/{task_id}/attempts").json()
    assert body["task_id"] == task_id
    assert body["attempts_used"] == 1
    assert body["max_attempts"] == 1
    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["attempt"] == 1


def test_attempts_are_redacted_at_rest(client, service, mock_provider) -> None:
    mock_provider.set_script(
        [{"ok": False, "stdout": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx"}]
    )
    task_id = create(client).json()["id"]
    service.wait_for_idle(10.0)

    body = client.get(f"/v1/tasks/{task_id}/attempts").json()
    persisted = body["attempts"][0]["stdout"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in persisted
    assert "[REDACTED]" in persisted

    # Also verify the raw on-disk payload is clean, not just the API response.
    task = service.store.get(task_id)
    assert "sk-abcdefghijklmnopqrstuvwx" not in (task.result_summary or "")
    for attempt in service.store.get_attempts(task_id):
        assert "sk-abcdefghijklmnopqrstuvwx" not in attempt.stdout


def test_cancel_completed_task_is_noop(client, service) -> None:
    task_id = create(client).json()["id"]
    service.wait_for_idle(10.0)
    resp = client.post(f"/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == TaskStatus.SUCCEEDED.value


def test_cancel_running_task(client, service, mock_provider) -> None:
    mock_provider.set_script([{"sleep": 2.0}])
    task_id = create(client, timeout_seconds=30.0).json()["id"]
    resp = client.post(f"/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == TaskStatus.CANCELLED.value
    assert resp.json()["error_class"] == "cancelled"
    assert service.wait_for_idle(15.0)


def test_cancel_missing_task_is_404(client) -> None:
    resp = client.post(f"/v1/tasks/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


def test_health_reports_lifecycle(client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["default_provider"] == "mock"
    assert body["store_open"] is True
    assert body["checkpoints_open"] is True
    assert body["allow_real_providers"] is False
    assert {p["name"] for p in body["providers"]} == {
        "mock",
        "minimax",
        "glm",
        "deepseek",
    }


def test_providers_endpoint(client) -> None:
    body = client.get("/v1/providers").json()
    assert body["default"] == "mock"
    mock = next(p for p in body["providers"] if p["name"] == "mock")
    assert mock["available"] is True
    minimax = next(p for p in body["providers"] if p["name"] == "minimax")
    assert minimax["provider_id"] == "minimax-cn"
    assert minimax["available"] is False


def test_reads_work_while_task_is_running(client, service, mock_provider) -> None:
    mock_provider.set_script([{"sleep": 1.5}])
    task_id = create(client, timeout_seconds=30.0).json()["id"]

    # The task is mid-flight; reads must still succeed promptly.
    for _ in range(5):
        assert client.get(f"/v1/tasks/{task_id}").status_code == 200
        assert client.get("/v1/tasks").status_code == 200
        assert client.get("/health").status_code == 200

    assert service.wait_for_idle(15.0)
    assert client.get(f"/v1/tasks/{task_id}").json()["status"] == "succeeded"

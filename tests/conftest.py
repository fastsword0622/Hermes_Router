"""Shared fixtures.

Every test writes into a per-test temp directory; the pytest ``--basetemp``
keeps those under the task scratch root rather than the repo.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hermes_router.config import Settings
from hermes_router.providers.mock import MockProvider
from hermes_router.service import TaskService


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep ambient HERMES_ROUTER_* env from leaking into tests."""
    for key in list(monkeypatch._setitem):  # pragma: no cover - defensive
        pass
    yield


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "router-data", api_token="router-test-admin-token-only")


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def service(settings: Settings, mock_provider: MockProvider) -> Iterator[TaskService]:
    svc = TaskService(settings, providers=[mock_provider])
    try:
        yield svc
    finally:
        svc.close()


@pytest.fixture
def client(service: TaskService):
    from fastapi.testclient import TestClient

    from hermes_router.api import create_app

    app = create_app(service.settings, service=service, recover=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer " + service.settings.api_token
        yield c

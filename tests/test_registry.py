"""Registry behaviour and the mock-first guarantee."""

from __future__ import annotations

import pytest

from hermes_router.registry import (
    DEFAULT_PROVIDER,
    REAL_PROVIDER_NAMES,
    get_spec,
    is_real_provider,
    list_specs,
)
from hermes_router.service import ProviderNotAvailable, TaskService
from hermes_router.models import CreateTaskRequest


def test_default_provider_is_mock() -> None:
    assert DEFAULT_PROVIDER == "mock"
    assert get_spec(DEFAULT_PROVIDER).is_real is False


@pytest.mark.parametrize(
    ("name", "provider_id", "model_id"),
    [
        ("minimax", "minimax-cn", "MiniMax-M3"),
        ("glm", "zai", "glm-5.3"),
        ("deepseek", "deepseek", "deepseek-v4-pro"),
    ],
)
def test_verified_provider_model_pairs(name: str, provider_id: str, model_id: str) -> None:
    spec = get_spec(name)
    assert spec.provider_id == provider_id
    assert spec.model_id == model_id
    assert spec.is_real is True
    assert name in REAL_PROVIDER_NAMES


def test_lookup_is_case_insensitive_and_strips() -> None:
    assert get_spec("  MiniMax ").name == "minimax"


def test_unknown_provider_raises_with_known_names() -> None:
    with pytest.raises(KeyError) as exc:
        get_spec("nope")
    message = str(exc.value)
    assert "unknown provider" in message
    assert "mock" in message


def test_list_specs_sorted_and_complete() -> None:
    names = [s.name for s in list_specs()]
    assert names == sorted(names)
    assert {"mock", "minimax", "glm", "deepseek"} == set(names)


def test_is_real_provider_unknown_is_false() -> None:
    assert is_real_provider("nope") is False
    assert is_real_provider("mock") is False


def test_real_provider_rejected_without_injection(service: TaskService) -> None:
    for name in ("minimax", "glm", "deepseek"):
        assert service.provider_available(name) is False
        with pytest.raises(ProviderNotAvailable) as exc:
            service.create_task(CreateTaskRequest(prompt="hi", provider=name))
        assert "not available" in str(exc.value)
        assert "allow_real_providers" in str(exc.value)


def test_real_provider_rejected_even_if_injected_without_flag(
    settings, mock_provider
) -> None:
    from hermes_router.providers.hermes import HermesSubprocessProvider

    injected = HermesSubprocessProvider("minimax", executable="/nonexistent/hermes")
    svc = TaskService(settings, providers=[mock_provider, injected])
    try:
        # Injected but allow_real_providers is still False -> refused.
        assert svc.provider_available("minimax") is False
        with pytest.raises(ProviderNotAvailable):
            svc.create_task(CreateTaskRequest(prompt="hi", provider="minimax"))
    finally:
        svc.close()


def test_real_provider_available_only_with_both_adapter_and_flag(
    settings, mock_provider
) -> None:
    from hermes_router.providers.hermes import HermesSubprocessProvider

    settings.allow_real_providers = True
    injected = HermesSubprocessProvider("minimax", executable="/nonexistent/hermes")
    svc = TaskService(settings, providers=[mock_provider, injected])
    try:
        assert svc.provider_available("minimax") is True
        # Still not actually run: we only check resolution, not execution.
        assert svc._resolve_provider("minimax") is injected
        # Unknown real providers stay unavailable even with the flag on.
        assert svc.provider_available("glm") is False
    finally:
        svc.close()


def test_describe_providers_reports_availability(service: TaskService) -> None:
    by_name = {p["name"]: p for p in service.describe_providers()}
    assert by_name["mock"]["available"] is True
    assert by_name["minimax"]["available"] is False
    assert by_name["glm"]["provider_id"] == "zai"


def test_resolve_provider_unknown_name(service: TaskService) -> None:
    with pytest.raises(ProviderNotAvailable):
        service._resolve_provider("does-not-exist")
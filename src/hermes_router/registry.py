"""Provider registry.

Phase 1 constraint that this module enforces structurally:

    The *default* provider is ``mock`` and the API can never dispatch a real
    provider on its own.  Real providers (``minimax``, ``glm``, ``deepseek``)
    are resolvable only through an explicitly injected adapter instance --
    see ``hermes_router.service.TaskService`` which builds its provider map from
    injected ``ProtocolProvider`` objects, never from this table directly.

The registry maps a logical role name to the *verified* Hermes CLI provider id
and model id recorded in the Phase 0 environment audit
(``docs/environment-audit.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ProviderSpec",
    "DEFAULT_PROVIDER",
    "REAL_PROVIDER_NAMES",
    "get_spec",
    "list_specs",
    "is_real_provider",
]

#: The only provider the API may use without an explicit injected adapter.
DEFAULT_PROVIDER: Final[str] = "mock"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Verified Hermes provider/model pair plus Phase 1 guard rails."""

    name: str
    provider_id: str
    model_id: str
    is_real: bool
    description: str


_SPECS: Final[dict[str, ProviderSpec]] = {
    "mock": ProviderSpec(
        name="mock",
        provider_id="mock",
        model_id="mock-model",
        is_real=False,
        description="In-process deterministic provider used for all API traffic in Phase 1.",
    ),
    "minimax": ProviderSpec(
        name="minimax",
        provider_id="minimax-cn",
        model_id="MiniMax-M3",
        is_real=True,
        description="MiniMax via Hermes provider id 'minimax-cn'.",
    ),
    "glm": ProviderSpec(
        name="glm",
        provider_id="zai",
        model_id="glm-5.3",
        is_real=True,
        description="Zhipu GLM via Hermes provider id 'zai' (alias 'glm').",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        is_real=True,
        description="DeepSeek via Hermes provider id 'deepseek'.",
    ),
}

#: Names that would contact a real inference provider.
REAL_PROVIDER_NAMES: Final[frozenset[str]] = frozenset(
    name for name, spec in _SPECS.items() if spec.is_real
)


def get_spec(name: str) -> ProviderSpec:
    """Return the spec for ``name``.

    Lookup is exact and case-insensitive; unknown names raise ``KeyError`` so
    callers surface a 400 rather than silently falling back to a real provider.
    """
    try:
        return _SPECS[name.strip().lower()]
    except KeyError:
        raise KeyError(
            f"unknown provider {name!r}; known providers: {', '.join(sorted(_SPECS))}"
        ) from None


def list_specs() -> list[ProviderSpec]:
    """All known provider specs, sorted by name."""
    return [_SPECS[name] for name in sorted(_SPECS)]


def is_real_provider(name: str) -> bool:
    """True when ``name`` refers to a real (network-calling) provider."""
    try:
        return get_spec(name).is_real
    except KeyError:
        return False
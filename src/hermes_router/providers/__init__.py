"""Provider implementations: mock (default) and the real Hermes subprocess adapter."""

from __future__ import annotations

from .base import MAX_CAPTURE_BYTES, MAX_INPUT_BYTES, ProtocolProvider, ProviderError
from .hermes import HermesSubprocessProvider, build_argv
from .mock import MOCK_MARKER, MockProvider

__all__ = [
    "MAX_CAPTURE_BYTES",
    "MAX_INPUT_BYTES",
    "ProtocolProvider",
    "ProviderError",
    "HermesSubprocessProvider",
    "MockProvider",
    "MOCK_MARKER",
    "build_argv",
]
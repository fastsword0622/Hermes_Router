"""Provider protocol shared by the mock provider and the real Hermes adapter."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import AttemptResult

__all__ = [
    "ProtocolProvider",
    "MAX_CAPTURE_BYTES",
    "ProviderError",
]

#: Hard bound on captured stdout/stderr per attempt.  Providers must truncate
#: rather than accumulate unbounded subprocess output.
MAX_CAPTURE_BYTES = 64 * 1024

#: Hard bound on the stdin payload we will hand to a provider.
MAX_INPUT_BYTES = 256 * 1024


class ProviderError(RuntimeError):
    """Raised for adapter-level problems that are not subprocess exit codes."""

    def __init__(self, message: str, error_class: str = "adapter_error") -> None:
        super().__init__(message)
        self.error_class = error_class


@runtime_checkable
class ProtocolProvider(Protocol):
    """Minimal provider surface used by the execution graph.

    Implementations must return an :class:`AttemptResult` whose ``stdout`` and
    ``stderr`` are already redacted and length-bounded.  A nonzero exit is a
    failure (``ok=False``); an exit code of zero is *necessary but not
    sufficient* -- ``ok`` reflects provider-reported success, not the exit code.
    """

    name: str
    provider_id: str
    model_id: str

    def run(
        self,
        *,
        prompt: str,
        attempt: int,
        timeout_seconds: float,
        max_turns: int,
        thread_id: str,
    ) -> AttemptResult:
        """Execute one attempt and return a structured, redacted result."""
        ...
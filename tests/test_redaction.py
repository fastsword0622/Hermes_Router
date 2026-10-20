"""Secret redaction."""

from __future__ import annotations

import pytest

from hermes_router.redaction import REDACTED, redact_mapping, redact_text


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx",
        "export ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop",
        "password: hunter2hunter2",
        "MY_SECRET=topsecretvalue123",
        "DB_PASSWORD = correcthorse",
    ],
)
def test_credential_assignments_are_redacted(text: str) -> None:
    out = redact_text(text)
    assert REDACTED in out
    # The secret value itself must be gone.
    for value in ("sk-abcdefghijklmnopqrstuvwx", "sk-ant-abcdefghijklmnop",
                  "hunter2hunter2", "topsecretvalue123", "correcthorse"):
        assert value not in out


def test_bearer_header_redacted() -> None:
    out = redact_text("Authorization: Bearer abcdef1234567890xyz")
    assert REDACTED in out
    assert "abcdef1234567890xyz" not in out


def test_json_credential_field_redacted() -> None:
    out = redact_text('{"api_key": "supersecretvalue", "model": "MiniMax-M3"}')
    assert "supersecretvalue" not in out
    assert "MiniMax-M3" in out


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("token is ghp_abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwx"),
        ("slack xoxb-1234567890-abcdef", "xoxb-1234567890-abcdef"),
    ],
)
def test_vendor_prefixes_redacted(text: str, secret: str) -> None:
    out = redact_text(text)
    assert secret not in out
    assert REDACTED in out


def test_extra_values_redacted() -> None:
    out = redact_text("value is zzzSECRETVALUE999", extra_values=["zzzSECRETVALUE999"])
    assert "zzzSECRETVALUE999" not in out


def test_short_explicit_secret_values_are_redacted() -> None:
    assert redact_text("abc abc", extra_values=["abc"]) == "[REDACTED] [REDACTED]"


def test_environment_variable_values_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TEST_API_KEY", "envsecretsvalue12345")
    out = redact_text("leaked envsecretsvalue12345 here")
    assert "envsecretsvalue12345" not in out


def test_non_sensitive_env_not_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TEST_HOME", "/a/short/path")
    assert redact_text("/a/short/path") == "/a/short/path"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_input(value) -> None:
    assert redact_text(value) == ""


def test_plain_text_unchanged() -> None:
    text = "build succeeded in 12s\n2 warnings"
    assert redact_text(text) == text


def test_redact_mapping_masks_sensitive_keys() -> None:
    out = redact_mapping(
        {"api_key": "secretvalue123", "model": "glm-5.3", "nested": {"token": "abcdefghijkl"}}
    )
    assert out["api_key"] == REDACTED
    assert out["model"] == "glm-5.3"
    assert out["nested"]["token"] == REDACTED


def test_redact_mapping_preserves_non_strings() -> None:
    out = redact_mapping({"count": 3, "ok": True, "items": ["a", "b"]})
    assert out == {"count": 3, "ok": True, "items": ["a", "b"]}


@pytest.mark.parametrize("value", [None, {}])
def test_redact_mapping_empty(value) -> None:
    assert redact_mapping(value) == {}

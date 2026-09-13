"""Codex CLI adapter contract tests without live CLI or credentials."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pr_reliability_contracts import UsageCoverage
from pr_reliability_workers.agents import ModelRequest
from pr_reliability_workers.agents.codex_client import (
    CodexCliModelClient,
    _build_prompt,
    _extract_json,
    _parse_usage,
)


def _request() -> ModelRequest:
    return ModelRequest(
        instruction="Review this PR for correctness defects.",
        context="--- a/app.py\n+x = 1/0\n",
        output_schema={"type": "object", "properties": {"findings": {"type": "array"}}},
        idempotency_key="R" * 26 + ":" + "a" * 40 + ":analyze",
    )


def _mock_completed_output(json_body: str = '{"findings":[]}') -> str:
    return json_body


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        CodexCliModelClient(timeout_seconds=0)


def test_rejects_empty_executable() -> None:
    with pytest.raises(ValueError, match="executable name must not be empty"):
        CodexCliModelClient(codex_executable="   ")


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_instruction_schema_and_context() -> None:
    req = _request()
    prompt = _build_prompt(req)
    assert req.instruction in prompt
    assert req.context in prompt
    assert json.dumps(req.output_schema, separators=(",", ":")) in prompt


def test_build_prompt_does_not_include_idempotency_key() -> None:
    req = _request()
    # idempotency key is for deduplication only; it must not be sent to the CLI.
    prompt = _build_prompt(req)
    assert req.idempotency_key not in prompt


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output,expected",
    [
        ('{"findings":[]}', '{"findings":[]}'),
        ('Log line\n{"findings":[]}', '{"findings":[]}'),
        ('{"findings":[{"claim":"x"}]}', '{"findings":[{"claim":"x"}]}'),
        ('{"a":1} trailing garbage', '{"a":1}'),
        ("no json here", None),
        ("", None),
        ("{broken", None),
    ],
)
def test_extract_json(output: str, expected: str | None) -> None:
    assert _extract_json(output) == expected


# ---------------------------------------------------------------------------
# _parse_usage
# ---------------------------------------------------------------------------


def test_parse_usage_returns_unknown_on_empty_output() -> None:
    usage = _parse_usage("")
    assert usage.coverage is UsageCoverage.UNKNOWN
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None
    assert usage.reported_cost_usd_micros is None


def test_parse_usage_extracts_full_usage_from_json_line() -> None:
    output = '{"usage":{"input_tokens":120,"output_tokens":45,"total_tokens":165}}'
    usage = _parse_usage(output)
    assert usage.coverage is UsageCoverage.FULL
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 45
    assert usage.total_tokens == 165
    assert usage.reported_cost_usd_micros is None  # Codex never reports cost


def test_parse_usage_returns_unknown_on_missing_usage_key() -> None:
    output = '{"findings":[]}'
    usage = _parse_usage(output)
    assert usage.coverage is UsageCoverage.UNKNOWN


def test_parse_usage_returns_partial_when_some_tokens_present() -> None:
    output = '{"usage":{"input_tokens":50}}'
    usage = _parse_usage(output)
    assert usage.coverage is UsageCoverage.PARTIAL
    assert usage.prompt_tokens == 50
    assert usage.completion_tokens is None
    assert usage.total_tokens is None


# ---------------------------------------------------------------------------
# complete() — subprocess interaction
# ---------------------------------------------------------------------------


def _make_proc(stdout: str = '{"findings":[]}', returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate.return_value = (stdout, "")
    proc.returncode = returncode
    proc.poll.return_value = returncode
    return proc


def test_complete_returns_model_response_on_success() -> None:
    with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
        client = CodexCliModelClient()
        response = client.complete(_request())

    assert response.output_json == '{"findings":[]}'
    assert response.usage.coverage is UsageCoverage.UNKNOWN
    call_args = mock_popen.call_args
    cmd = call_args[0][0]
    assert cmd[0] == "codex"
    assert "--approval-mode" in cmd
    assert "full-auto" in cmd
    assert "--quiet" in cmd


def test_complete_uses_custom_executable_and_timeout() -> None:
    with patch("subprocess.Popen", return_value=_make_proc()) as mock_popen:
        CodexCliModelClient(timeout_seconds=30.0, codex_executable="my-codex").complete(
            _request()
        )
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "my-codex"
    proc = mock_popen.return_value
    assert proc.communicate.call_args[1]["timeout"] == 30.0


def test_complete_raises_on_non_zero_exit() -> None:
    with patch("subprocess.Popen", return_value=_make_proc(returncode=1)):
        with pytest.raises(RuntimeError, match="non-zero status"):
            CodexCliModelClient().complete(_request())


def test_complete_raises_on_no_json_output() -> None:
    with patch("subprocess.Popen", return_value=_make_proc(stdout="no json here")):
        with pytest.raises(RuntimeError, match="no valid JSON output"):
            CodexCliModelClient().complete(_request())


def test_complete_raises_timeout_on_subprocess_timeout() -> None:
    proc = MagicMock()
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["codex"], timeout=10)
    with patch("subprocess.Popen", return_value=proc):
        with pytest.raises(RuntimeError, match="timed out"):
            CodexCliModelClient().complete(_request())
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()


def test_complete_raises_when_executable_not_found() -> None:
    with patch("subprocess.Popen", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="not available"):
            CodexCliModelClient().complete(_request())


def test_complete_captures_usage_when_reported() -> None:
    output = '{"findings":[]}\n{"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}'
    with patch("subprocess.Popen", return_value=_make_proc(stdout=output)):
        response = CodexCliModelClient().complete(_request())
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 15
    assert response.usage.coverage is UsageCoverage.FULL


# ---------------------------------------------------------------------------
# Security: errors must not leak credentials or context
# ---------------------------------------------------------------------------


def test_error_does_not_expose_context_or_credentials() -> None:
    """RuntimeError messages must never include the review context or any key material."""
    with patch("subprocess.Popen", return_value=_make_proc(returncode=1)):
        try:
            CodexCliModelClient().complete(_request())
        except RuntimeError as exc:
            error_text = str(exc)
            assert "--- a/app.py" not in error_text  # context fragment
            assert "Review this PR" not in error_text  # instruction
            assert "analyze" not in error_text  # idempotency key fragment


def test_error_does_not_expose_prompt_on_timeout() -> None:
    proc = MagicMock()
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["codex"], timeout=10)
    with patch("subprocess.Popen", return_value=proc):
        try:
            CodexCliModelClient().complete(_request())
        except RuntimeError as exc:
            assert "--- a/app.py" not in str(exc)
            assert "Review this PR" not in str(exc)


# ---------------------------------------------------------------------------
# Factory wiring: MODEL_PROVIDER=codex must not require OPENAI_API_KEY
# ---------------------------------------------------------------------------


def test_factory_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_operations must reject any provider other than openai or codex."""
    import os

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("OWNER_ID", "O" * 26)
    monkeypatch.setenv("SANDBOX_STAGING_DIRECTORY", "/tmp")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", "/tmp/key.pem")
    monkeypatch.setenv("REVIEW_CHECK_ALLOWLIST_JSON", "[]")

    from pr_reliability_workers.providers.factory import create_operations

    with pytest.raises(RuntimeError, match="MODEL_PROVIDER must be openai or codex"):
        create_operations()

"""Codex CLI adapter for the provider-neutral model boundary.

Runs the Codex CLI non-interactively in a subprocess and returns structured
JSON output. This adapter is not enabled in the production worker: CLI read-only
mode cannot prevent untrusted prompts from reading host secrets.

See docs/configuration.md for deployment constraints.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pr_reliability_contracts import ModelUsage, UsageCoverage

from .model_client import ModelRequest, ModelResponse

_DEFAULT_TIMEOUT = 180.0


class CodexInvocation:
    """One review's cancellation state, never shared with another invocation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = False

    def register(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            if self._cancelled:
                _kill_group(process)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._process is not None:
                _kill_group(self._process)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def clear(self) -> None:
        with self._lock:
            self._process = None


_INVOCATION: ContextVar[CodexInvocation | None] = ContextVar("codex_invocation", default=None)


@contextmanager
def codex_invocation(invocation: CodexInvocation) -> Iterator[None]:
    """Bind cancellation to just this review in the worker thread."""
    token = _INVOCATION.set(invocation)
    try:
        yield
    finally:
        _INVOCATION.reset(token)


class CodexCliModelClient:
    """Non-interactive, ephemeral Codex CLI execution.

    The client passes instruction and context to ``codex`` and parses JSON output.
    No conversation state is retained. The CLI can read authentication files and
    other host files, so the production factory must refuse to create this adapter.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        codex_executable: str = "codex",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Codex CLI timeout must be positive")
        if not codex_executable or not codex_executable.strip():
            raise ValueError("Codex CLI executable name must not be empty")
        self._timeout = timeout_seconds
        self._executable = codex_executable.strip()

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one non-interactive Codex review and return structured output.

        Raises RuntimeError on any subprocess, timeout, or output error.
        The error message never includes the context, instruction, or any
        credential material.
        """
        if os.name != "posix":
            raise RuntimeError("Codex CLI requires an isolated POSIX runner")
        prompt = _build_prompt(request)
        cmd = [
            self._executable,
            "exec",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--",
            prompt,
        ]
        proc = None
        invocation = _INVOCATION.get()
        if invocation is not None and invocation.cancelled:
            raise RuntimeError("Codex CLI review was cancelled")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_safe_env(),
                start_new_session=True,
            )
            if invocation is not None:
                invocation.register(proc)
            deadline = time.monotonic() + self._timeout
            while True:
                try:
                    stdout, _ = proc.communicate(
                        timeout=min(0.1, max(0, deadline - time.monotonic()))
                    )
                    break
                except subprocess.TimeoutExpired:
                    # A descendant may hold the output pipes after the CLI itself exits.
                    if proc.poll() is not None:
                        _terminate_group(proc)
                        stdout, _ = proc.communicate(timeout=max(0, deadline - time.monotonic()))
                        break
                    if time.monotonic() >= deadline:
                        raise
            _terminate_group(proc)
        except subprocess.TimeoutExpired:
            if proc is not None:
                _terminate_group(proc)
            raise RuntimeError("Codex CLI timed out") from None
        except OSError:
            if proc is not None:
                _terminate_group(proc)
            raise RuntimeError("Codex CLI is not available") from None
        except Exception:  # noqa: BLE001 - sanitize arbitrary subprocess errors
            if proc is not None:
                _terminate_group(proc)
            raise RuntimeError("Codex CLI failed") from None
        except BaseException:
            if proc is not None:
                _terminate_group(proc)
            raise
        finally:
            if invocation is not None:
                invocation.clear()

        if invocation is not None and invocation.cancelled:
            raise RuntimeError("Codex CLI review was cancelled")
        if proc.returncode != 0:
            raise RuntimeError("Codex CLI exited with a non-zero status") from None

        output_text = _extract_json(stdout)
        if output_text is None:
            raise RuntimeError("Codex CLI produced no valid JSON output") from None

        usage = _parse_usage(stdout)
        return ModelResponse(output_json=output_text, usage=usage)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _terminate_group(proc: subprocess.Popen[str]) -> None:
    """Stop descendants even if the CLI itself exited, then reap the direct child."""
    try:
        _kill_group(proc)
    finally:
        proc.wait()


def _build_prompt(request: ModelRequest) -> str:
    """Compose a single non-interactive prompt from instruction, schema, and context.

    The prompt instructs Codex to return JSON matching ``output_schema`` so the
    review agent can validate the structured output without any further parsing.
    """
    schema_text = json.dumps(request.output_schema, separators=(",", ":"))
    return (
        f"{request.instruction}\n\n"
        f"Return ONLY a JSON object matching this schema (no markdown, no explanation):\n"
        f"{schema_text}\n\n"
        f"Context:\n{request.context}"
    )


def _extract_json(output: str) -> str | None:
    """Find the first complete JSON object in stdout.

    Codex CLI may emit log lines before the JSON block.  We locate the first
    ``{`` and try to parse from there, skipping any leading non-JSON lines.
    """
    if not output:
        return None
    start = output.find("{")
    if start == -1:
        return None
    candidate = output[start:].strip()
    # Remove any trailing content after the outermost JSON object.
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i, ch in enumerate(candidate):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    extracted = candidate[: end + 1]
    # Validate it is parseable JSON before returning.
    try:
        json.loads(extracted)
    except (json.JSONDecodeError, ValueError):
        return None
    return extracted


def _parse_usage(output: str) -> ModelUsage:
    """Extract token counts from Codex CLI output when present.

    Codex CLI may print a usage summary like:
        {"usage": {"input_tokens": 120, "output_tokens": 45}}

    When no usage data is found, all fields remain Unknown per project rules.
    """
    if not output:
        return ModelUsage(schema_version="1", coverage=UsageCoverage.UNKNOWN)
    # Look for a JSON block that contains a "usage" key.
    try:
        parsed: Any = json.loads(output.strip())
        if isinstance(parsed, dict) and "usage" in parsed:
            raw = parsed["usage"]
            return _usage_from_dict(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    # Try to find usage JSON embedded in mixed output.
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "usage" in parsed:
                return _usage_from_dict(parsed["usage"])
        except (json.JSONDecodeError, ValueError):
            continue
    return ModelUsage(schema_version="1", coverage=UsageCoverage.UNKNOWN)


def _usage_from_dict(raw: Any) -> ModelUsage:
    if not isinstance(raw, dict):
        return ModelUsage(schema_version="1", coverage=UsageCoverage.UNKNOWN)
    input_tokens = _nonneg_int(raw.get("input_tokens"))
    output_tokens = _nonneg_int(raw.get("output_tokens"))
    total_tokens = _nonneg_int(raw.get("total_tokens"))
    known = sum(v is not None for v in (input_tokens, output_tokens, total_tokens))
    coverage = (
        UsageCoverage.UNKNOWN
        if known == 0
        else UsageCoverage.FULL
        if known == 3
        else UsageCoverage.PARTIAL
    )
    return ModelUsage(
        schema_version="1",
        coverage=coverage,
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        reported_cost_usd_micros=None,
    )


def _nonneg_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_env() -> dict[str, str]:
    """Pass only executable lookup, locale, and CLI authentication location.

    This does not isolate the host filesystem; production creation remains disabled.
    """
    return {
        key: os.environ[key]
        for key in ("PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "SSL_CERT_FILE")
        if key in os.environ
    }

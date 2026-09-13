"""Codex CLI adapter for the provider-neutral model boundary.

Runs the Codex CLI non-interactively in a subprocess and returns structured
JSON output.  Suitable only for trusted private pilot repositories where the
operator has a ChatGPT subscription and the Codex CLI is installed on the
activity-worker host.

See docs/configuration.md for deployment constraints.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from pr_reliability_contracts import ModelUsage, UsageCoverage

from .model_client import ModelClient, ModelRequest, ModelResponse

_DEFAULT_TIMEOUT = 180.0


class CodexCliModelClient:
    """Non-interactive, ephemeral Codex CLI execution.

    The client passes the instruction and context to ``codex`` as a single
    read-only prompt and parses its JSON output.  No conversation state is
    retained between calls.  Credentials are read from the environment by the
    CLI itself; this class never reads or logs them.
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
        prompt = _build_prompt(request)
        cmd = [
            self._executable,
            "--approval-mode", "full-auto",
            "--quiet",
            "--no-git",
            "--",
            prompt,
        ]
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_safe_env(),
            )
            stdout, _ = proc.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            if proc is not None:
                proc.kill()
                proc.wait()
            raise RuntimeError("Codex CLI timed out") from None
        except (OSError, FileNotFoundError):
            raise RuntimeError("Codex CLI is not available") from None
        except BaseException:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            raise RuntimeError("Codex CLI failed to start") from None

        if proc.returncode != 0:
            raise RuntimeError("Codex CLI exited with a non-zero status") from None

        output_text = _extract_json(stdout)
        if output_text is None:
            raise RuntimeError("Codex CLI produced no valid JSON output") from None

        usage = _parse_usage(stdout)
        return ModelResponse(output_json=output_text, usage=usage)


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
    """Return an execution environment stripped of sensitive platform credentials.

    Removes platform database credentials, GitHub App private keys/tokens, and
    direct provider API keys so they cannot be accessed by Codex prompts.
    """
    env = dict(os.environ)
    sensitive_prefixes = ("DATABASE_", "POSTGRES_", "GITHUB_PRIVATE_", "GITHUB_APP_")
    sensitive_keys = {
        "OPENAI_API_KEY",
        "DATABASE_URL",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_PRIVATE_KEY_PATH",
    }
    for key in list(env.keys()):
        if key in sensitive_keys or any(key.startswith(p) for p in sensitive_prefixes):
            env.pop(key, None)
    return env


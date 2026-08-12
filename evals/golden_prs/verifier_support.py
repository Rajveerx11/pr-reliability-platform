"""Run candidate probes outside the protected verifier process."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

OUTPUT_LIMIT = 8_000


def run_probe(workspace: Path, source: str) -> Any:
    """Run one probe and read only its parent-selected result channel."""

    with tempfile.TemporaryDirectory(prefix="golden-result-") as temp:
        result_path = Path(temp) / "result.json"
        environment = _safe_environment()
        environment["GOLDEN_RESULT_PATH"] = str(result_path)
        completed = subprocess.run(
            [sys.executable, "-I", "-c", source, str(workspace)],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            raise AssertionError(f"candidate probe failed: {completed.stderr[:OUTPUT_LIMIT]}")
        if len(completed.stdout) > OUTPUT_LIMIT or len(completed.stderr) > OUTPUT_LIMIT:
            raise AssertionError("candidate probe output exceeded limit")
        try:
            raw_result = result_path.read_text(encoding="utf-8")
            return json.loads(raw_result)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AssertionError("candidate probe did not complete parent result") from exc


def run_module_probe(workspace: Path, filename: str, assertions: str) -> Any:
    """Load one candidate module in the child, then run task-specific checks."""

    source = f"""
import importlib.util
import json
import os
import sys
from pathlib import Path

result_path = os.environ.pop("GOLDEN_RESULT_PATH")
open_result = os.open
write_result = os.write
close_result = os.close
encode_result = json.dumps
path = Path(sys.argv[1]) / {filename!r}
spec = importlib.util.spec_from_file_location("candidate", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
{assertions}
payload = encode_result(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
descriptor = open_result(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    write_result(descriptor, payload)
finally:
    close_result(descriptor)
"""
    return run_probe(workspace, source)


def _safe_environment() -> dict[str, str]:
    keys = ["PATH"]
    if os.name == "nt":
        keys.extend(["SYSTEMROOT", "COMSPEC"])
    return {key: os.environ[key] for key in keys if key in os.environ}

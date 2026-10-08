"""Read-only bounded report-file export from a live disposable container."""

import re

# Keep configuration validation stdlib-only: release preflight imports it with python -S.
MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORT_FILES = 4

_REPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.xml\Z")
# Image-owned Python runs isolated (-I); neither workspace imports nor repository code run.
# Read an opened regular file, not a path after checking it. No archive or host file writes.
_EXPORT_SCRIPT = """
import os, stat, sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        limit = int(sys.argv[2])
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
            raise ValueError()
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or after.st_nlink != 1 or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns:
            raise ValueError()
    sys.stdout.buffer.write(raw)
except Exception:
    sys.exit(1)
"""


def validate_report_files(names: tuple[str, ...]) -> None:
    # Only direct /workspace children: no attacker-controlled ancestor can be followed.
    if not isinstance(names, tuple) or len(names) > MAX_REPORT_FILES:
        raise ValueError("invalid approved report files")
    if any(not isinstance(name, str) or not _REPORT_NAME.fullmatch(name) for name in names):
        raise ValueError("reports must be XML basenames directly in /workspace")
    if len(set(names)) != len(names):
        raise ValueError("duplicate approved report files")


async def export_reports(runtime, container_name: str, names: tuple[str, ...]):
    from pr_reliability_evidence import ReportError, summarize_junit

    summaries = []
    for name in names:
        try:
            result = await runtime.execute(
                (
                    "exec",
                    container_name,
                    "python3",
                    "-I",
                    "-c",
                    _EXPORT_SCRIPT,
                    f"/workspace/{name}",
                    str(MAX_REPORT_BYTES),
                ),
                timeout_seconds=10,
                output_limit_bytes=MAX_REPORT_BYTES,
            )
            if result.return_code != 0 or result.timed_out or result.output_limit_exceeded:
                raise ReportError("report_invalid")
            summaries.append(summarize_junit(result.stdout))
        except (ReportError, RuntimeError, OSError):
            # Do not include runtime diagnostics, report content, or paths in product data.
            return (), "report_invalid"
    return tuple(summaries), None

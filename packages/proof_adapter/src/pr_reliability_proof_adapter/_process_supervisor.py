"""Run the Python gate while keeping its process-tree root alive for cleanup."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    """Wait for lifecycle setup, run the gate, report its exit, then stay alive."""
    if len(sys.argv) < 5 or sys.stdin.buffer.read(1) != b"1":
        raise SystemExit(125)
    status_path = Path(sys.argv[1])
    exit_code = _run_python(sys.argv[2:])
    sys.stdout.flush()
    sys.stderr.flush()
    _write_status(status_path, exit_code)
    sys.stdin.buffer.read(1)
    raise SystemExit(exit_code)


def _write_status(status_path: Path, exit_code: int) -> None:
    temporary_status = status_path.with_suffix(".tmp")
    descriptor = os.open(
        temporary_status,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="ascii") as status_file:
        status_file.write(str(exit_code))
    os.replace(temporary_status, status_path)


def _run_python(arguments: list[str]) -> int:
    if Path(arguments[0]).resolve() != Path(sys.executable).resolve() or len(arguments) < 3:
        return 126
    mode = arguments[1]
    target = arguments[2]
    sys.argv = [target, *arguments[3:]]
    try:
        if mode == "-m":
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        elif mode == "-c":
            namespace = {"__name__": "__main__", "__builtins__": __builtins__}
            exec(compile(target, "<proof-worker>", "exec"), namespace)  # noqa: S102
        else:
            return 126
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else int(exc.code is not None)
    except BaseException:  # noqa: BLE001 - gate internals must not cross the boundary
        return 1
    return 0


if __name__ == "__main__":
    main()

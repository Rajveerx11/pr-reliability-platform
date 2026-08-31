"""Run the Python gate while keeping its process-tree root alive for cleanup."""

from __future__ import annotations

import ctypes
import os
import runpy
import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Wait for lifecycle setup, run the gate, report its exit, then stay alive."""
    if len(sys.argv) < 6:
        raise SystemExit(125)
    status_path = Path(sys.argv[1])
    ready_path = Path(sys.argv[2])
    if os.name == "posix":
        _enable_linux_subreaper()
    _write_status(ready_path, 1)
    start_command = sys.stdin.buffer.read(1)
    if os.name == "posix" and start_command == b"2":
        _reap_children()
        raise SystemExit(0)
    if start_command != b"1":
        raise SystemExit(125)
    if os.name == "posix":
        exit_code = _run_python_child(sys.argv[3:])
    else:
        exit_code = _run_python(sys.argv[3:])
    sys.stdout.flush()
    sys.stderr.flush()
    _write_status(status_path, exit_code)
    if os.name == "posix":
        if sys.stdin.buffer.read(1) != b"2":
            raise SystemExit(125)
        _reap_children()
        raise SystemExit(0)
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
    if not _valid_python_arguments(arguments):
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


def _run_python_child(arguments: list[str]) -> int:
    if not _valid_python_arguments(arguments):
        return 126
    completed = subprocess.run(arguments, stdin=subprocess.DEVNULL, check=False)
    return completed.returncode


def _valid_python_arguments(arguments: list[str]) -> bool:
    return (
        len(arguments) >= 3
        and Path(arguments[0]).resolve() == Path(sys.executable).resolve()
        and arguments[1] in {"-m", "-c"}
    )


def _reap_children() -> None:
    while True:
        try:
            process_id, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if process_id == 0:
            raise RuntimeError("proof descendants remain alive during cleanup")


def _enable_linux_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("proof descendant supervision requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "could not enable proof descendant supervision")


if __name__ == "__main__":
    main()

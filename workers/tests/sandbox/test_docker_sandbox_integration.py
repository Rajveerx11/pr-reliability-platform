"""Real Docker checks for disposable workspace and network isolation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pr_reliability_workers.sandbox import (
    DockerSandboxRunner,
    SandboxLimits,
    SandboxRequest,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_SANDBOX_TESTS") != "1",
    reason="real Docker sandbox tests run in their dedicated CI job",
)


def test_real_engine_passes_required_capability_preflight(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(image=image, workspace=tmp_path, command=("true",))
        )
    )

    assert result.succeeded, result.stderr


def test_real_container_has_no_network_and_destroys_writable_workspace(
    tmp_path: Path,
) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]
    (tmp_path / "input.txt").write_text("fixture", encoding="utf-8")
    script = """
import pathlib
import socket

assert pathlib.Path('input.txt').read_text() == 'fixture'
pathlib.Path('result.txt').write_text('container-only')
sock = socket.socket()
sock.settimeout(0.25)
try:
    sock.connect(('1.1.1.1', 53))
except OSError:
    print('network-blocked')
else:
    raise SystemExit('network unexpectedly available')
finally:
    sock.close()
"""

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=("python", "-c", script),
                limits=SandboxLimits(timeout_seconds=10),
            )
        )
    )

    assert result.succeeded, result.stderr
    assert result.stdout == "network-blocked\n"
    assert not (tmp_path / "result.txt").exists()


def test_real_container_is_killed_on_timeout(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=("python", "-c", "import time; time.sleep(10)"),
                limits=SandboxLimits(timeout_seconds=0.2),
            )
        )
    )

    assert result.timed_out
    assert result.exit_code is None
    assert not result.succeeded


def test_real_container_stops_at_output_limit(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=("python", "-c", "print('x' * 100000)"),
                limits=SandboxLimits(output_bytes=1024),
            )
        )
    )

    assert result.output_limit_exceeded
    assert len(result.stdout.encode()) + len(result.stderr.encode()) == 1024
    assert not result.succeeded


def test_real_container_enforces_workspace_tmpfs_limit(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]
    script = """
import pathlib

try:
    with pathlib.Path('large.bin').open('wb') as output:
        for _ in range(64):
            output.write(b'x' * 65536)
except OSError:
    print('workspace-blocked')
else:
    raise SystemExit('workspace limit not enforced')
"""

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=("python", "-c", script),
                limits=SandboxLimits(workspace_bytes=1024 * 1024),
            )
        )
    )

    assert result.succeeded, result.stderr
    assert result.stdout == "workspace-blocked\n"


def test_real_container_enforces_pid_limit(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]
    script = """
import os
import signal
import time

children = []
try:
    for _ in range(64):
        child = os.fork()
        if child == 0:
            time.sleep(30)
            os._exit(0)
        children.append(child)
except OSError:
    print('pids-blocked')
else:
    raise SystemExit('PID limit not enforced')
finally:
    for child in children:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in children:
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
"""

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=("python", "-c", script),
                limits=SandboxLimits(pids=8),
            )
        )
    )

    assert result.succeeded, result.stderr
    assert result.stdout == "pids-blocked\n"


def test_real_container_enforces_memory_limit(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=(
                    "python",
                    "-c",
                    "value = bytearray(128 * 1024 * 1024); print(len(value))",
                ),
                limits=SandboxLimits(memory_bytes=32 * 1024 * 1024),
            )
        )
    )

    assert not result.succeeded
    assert not result.timed_out
    assert not result.output_limit_exceeded
    assert result.exit_code not in {None, 0}


def test_real_container_has_cpu_cgroup_quota(tmp_path: Path) -> None:
    image = os.environ["SANDBOX_TEST_IMAGE"]
    script = """
import pathlib

v2 = pathlib.Path('/sys/fs/cgroup/cpu.max')
if v2.exists():
    quota_text, period_text = v2.read_text().split()
    assert quota_text != 'max'
    ratio = int(quota_text) / int(period_text)
else:
    root = pathlib.Path('/sys/fs/cgroup/cpu')
    quota = int((root / 'cpu.cfs_quota_us').read_text())
    period = int((root / 'cpu.cfs_period_us').read_text())
    ratio = quota / period
assert 0 < ratio <= 0.26, ratio
print('cpu-capped')
"""

    result = asyncio.run(
        DockerSandboxRunner().run(
            SandboxRequest(
                image=image,
                workspace=tmp_path,
                command=("python", "-c", script),
                limits=SandboxLimits(cpu_count=0.25),
            )
        )
    )

    assert result.succeeded, result.stderr
    assert result.stdout == "cpu-capped\n"

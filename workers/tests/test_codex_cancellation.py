"""Cancellation of an active Codex review must stop its process tree."""

from __future__ import annotations

import asyncio
import os
import shlex
import threading
from pathlib import Path

import pytest
from pr_reliability_workers.agents import ModelRequest, ReviewAgent
from pr_reliability_workers.agents.codex_client import CodexCliModelClient
from pr_reliability_workers.providers.operations import ProductionOperations
from pr_reliability_workers.workflows.types import StageRequest


def _assert_dead_or_zombie(pid: int) -> None:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except FileNotFoundError:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    else:
        assert state == "Z"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_successful_cli_exit_stops_remaining_descendants(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    cli = tmp_path / "fake-codex"
    cli.write_text(
        "#!/bin/sh\n"
        "sleep 60 &\n"
        f"echo $! > {shlex.quote(str(child_pid))}\n"
        "echo '{\"findings\":[]}'\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    client = CodexCliModelClient(timeout_seconds=2, codex_executable=str(cli))
    response = client.complete(
        ModelRequest(
            instruction="review",
            context="sample",
            output_schema={"type": "object"},
            idempotency_key="test",
        )
    )
    assert response.output_json == '{"findings":[]}'
    _assert_dead_or_zombie(int(child_pid.read_text(encoding="utf-8")))


def test_repeated_cancellation_waits_for_review_thread(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowReviewer:
        _client = CodexCliModelClient()

        def review(self, *_: object) -> None:
            started.set()
            release.wait(5)
            finished.set()

    operations = ProductionOperations(
        connection_factory=lambda: None,  # type: ignore[arg-type]
        checkout=None,  # type: ignore[arg-type]
        reviewer=SlowReviewer(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        check_policy=None,  # type: ignore[arg-type]
        id_factory=lambda: "unused",
    )

    async def run() -> None:
        task = asyncio.create_task(operations._review_codex(None, ""))  # type: ignore[arg-type]
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert finished.is_set()

    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_cancel_analyze_kills_child_and_reaps_cli(tmp_path: Path) -> None:
    shell_pid = tmp_path / "shell.pid"
    child_pid = tmp_path / "child.pid"
    cli = tmp_path / "fake-codex"
    cli.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {shlex.quote(str(shell_pid))}\n"
        "sleep 60 &\n"
        f"echo $! > {shlex.quote(str(child_pid))}\n"
        "wait\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    context = tmp_path / "context.txt"
    context.write_text("changed review context", encoding="utf-8")
    operations = ProductionOperations(
        connection_factory=lambda: None,  # type: ignore[arg-type]
        checkout=None,  # type: ignore[arg-type]
        reviewer=ReviewAgent(CodexCliModelClient(timeout_seconds=30, codex_executable=str(cli))),
        workspace_root=tmp_path,
        check_policy=None,  # type: ignore[arg-type]
        id_factory=lambda: "unused",
    )
    operations._load_active_run = lambda *_: object()  # type: ignore[method-assign]
    operations._stage_receipt = lambda *_: None  # type: ignore[method-assign]
    operations._artifacts_ready = lambda *_: True  # type: ignore[method-assign]
    operations._context_path = lambda *_: context  # type: ignore[method-assign]
    run_id = "01J00000000000000000000001"
    head_sha = "a" * 40
    request = StageRequest(
        owner_id=run_id,
        run_id=run_id,
        head_sha=head_sha,
        input_ref=f"context:{run_id}:{head_sha}",
        idempotency_key=f"{run_id}:{head_sha}:analyze",
    )

    async def run() -> None:
        task = asyncio.create_task(operations.analyze(request))
        async with asyncio.timeout(5):
            while not child_pid.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    parent = int(shell_pid.read_text(encoding="utf-8"))
    child = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(parent, 0)  # Direct child was reaped by the worker thread.
    # A killed descendant may briefly remain as a zombie until the OS init process reaps it.
    _assert_dead_or_zombie(child)

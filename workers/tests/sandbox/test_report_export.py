"""Report-file export rejects links and never opens arbitrary host paths."""

import asyncio
import os
import subprocess
import sys

import pytest
from pr_reliability_evidence import MAX_REPORT_BYTES
from pr_reliability_workers.sandbox import DockerSandboxRunner, SandboxRequest
from pr_reliability_workers.sandbox.reports import _EXPORT_SCRIPT
from test_docker_sandbox import ENGINE_CAPABILITIES, IMAGE, ScriptedRuntime, runtime_result

REPORT = b'<testsuite><testcase time=".5"/></testsuite>'


@pytest.mark.parametrize(
    "name",
    ["../junit.xml", "/etc/passwd.xml", "reports/junit.xml", "a\\junit.xml", "..", "junit.txt"],
)
def test_only_operator_approved_direct_workspace_xml_basenames(tmp_path, name):
    with pytest.raises(ValueError):
        SandboxRequest(IMAGE, tmp_path, ("true",), report_files=(name,))


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "oversized", "empty", "regular"]
)
def test_trusted_exporter_opens_only_one_bounded_regular_unlinked_file(tmp_path, kind):
    report = tmp_path / "junit.xml"
    original = tmp_path / "original.xml"
    original.write_bytes(REPORT)
    if kind == "symlink":
        try:
            report.symlink_to(original)
        except OSError:
            pytest.skip("OS requires symlink privilege; real Linux sandbox CI covers this case")
    elif kind == "hardlink":
        os.link(original, report)
    elif kind == "directory":
        report.mkdir()
    else:
        report.write_bytes(
            b"x" * (MAX_REPORT_BYTES + 1)
            if kind == "oversized"
            else b""
            if kind == "empty"
            else REPORT
        )
    # The exporter is Linux-only (O_NOFOLLOW); run its actual descriptor checks on Linux.
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW exporter needs Linux; dedicated real sandbox CI covers it")
    result = subprocess.run(
        (sys.executable, "-I", "-c", _EXPORT_SCRIPT, str(report), str(MAX_REPORT_BYTES)),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == (0 if kind == "regular" else 1)
    assert result.stdout == (REPORT if kind == "regular" else b"")
    assert result.stderr == b""


def test_live_file_export_precedes_container_removal(tmp_path):
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=b"log", stderr=b"warning"),
        runtime_result(stdout=REPORT),
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=b"linux\n"),
    )
    result = asyncio.run(
        DockerSandboxRunner(runtime).run(
            SandboxRequest(IMAGE, tmp_path, ("test-command",), report_files=("junit.xml",))
        )
    )
    assert result.succeeded
    assert result.report_summaries == (
        {"passed": 1, "failed": 0, "skipped": 0, "duration_ms": 500},
    )
    assert [call.arguments[0] for call in runtime.calls] == [
        "info",
        "create",
        "start",
        "exec",
        "exec",
        "rm",
        "container",
        "info",
    ]
    assert runtime.calls[4].arguments[-2:] == ("/workspace/junit.xml", str(MAX_REPORT_BYTES))
    assert runtime.calls[4].output_limit_bytes == MAX_REPORT_BYTES
    assert runtime.calls[4].timeout_seconds == 10
    assert runtime.calls[1].arguments[-1] == "exec sleep 1200"


@pytest.mark.parametrize(
    "export",
    [
        runtime_result(return_code=1, stderr=b"sensitive path"),
        runtime_result(stdout=b"<broken>"),
        runtime_result(stdout=b"<xml>", output_limit_exceeded=True),
        runtime_result(timed_out=True),
    ],
)
def test_bad_export_blocks_verification_and_still_removes_container(tmp_path, export):
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(),
        runtime_result(),
        runtime_result(),
        export,
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=b"linux\n"),
    )
    result = asyncio.run(
        DockerSandboxRunner(runtime).run(
            SandboxRequest(IMAGE, tmp_path, ("test-command",), report_files=("junit.xml",))
        )
    )
    assert not result.succeeded
    assert result.report_error == "report_invalid"
    assert result.report_summaries == ()
    assert runtime.calls[5].arguments[0] == "rm"


def test_report_failures_block_successful_exit_code(tmp_path):
    report = b"<testsuite><testcase><failure/></testcase></testsuite>"
    runtime = ScriptedRuntime(
        runtime_result(stdout=ENGINE_CAPABILITIES),
        runtime_result(),
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=report),
        runtime_result(),
        runtime_result(),
        runtime_result(stdout=b"linux\n"),
    )
    result = asyncio.run(
        DockerSandboxRunner(runtime).run(
            SandboxRequest(IMAGE, tmp_path, ("test-command",), report_files=("junit.xml",))
        )
    )
    assert not result.succeeded
    assert result.report_summaries[0]["failed"] == 1

"""Production verification persists ciphertext and safe idempotent receipts atomically."""

import asyncio
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pr_reliability_evidence import EvidenceSettings
from pr_reliability_workers.activities import VerificationCheckEvidence, VerificationEvidence
from pr_reliability_workers.providers.operations import ProductionOperations
from pr_reliability_workers.sandbox import SandboxResult
from pr_reliability_workers.workflows.types import StageRequest
from test_production_operations import OWNER_ID, RUN_ID, _check_policy, _seed, connection_factory

__all__ = ["connection_factory"]


def test_verification_artifacts_are_encrypted_redacted_bounded_and_idempotent(connection_factory):
    head = "b" * 40
    _seed(connection_factory, "a" * 40, head)
    with connection_factory() as connection:
        connection.execute("UPDATE runs SET state = 'verifying' WHERE public_id = %s", (RUN_ID,))
    settings = EvidenceSettings(
        Fernet.generate_key(), max_bytes=4096, secret_patterns=("secret-value",)
    )
    sequence = iter(range(100, 200))
    operations = ProductionOperations(
        connection_factory,
        None,
        None,
        Path("."),
        _check_policy(),
        lambda: f"01J{next(sequence):023d}",
        settings,
    )
    request = StageRequest(
        owner_id=OWNER_ID, run_id=RUN_ID, head_sha=head, idempotency_key=f"{RUN_ID}:{head}:verify"
    )
    evidence = VerificationEvidence(
        checks=(
            VerificationCheckEvidence(
                "python-tests",
                "failed",
                "path_match",
                SandboxResult(
                    1,
                    "secret-value " + "x" * 10000,
                    "secret-value stderr",
                    25,
                    report_summaries=({"passed": 1, "failed": 1, "skipped": 0, "duration_ms": 25},),
                ),
            ),
            VerificationCheckEvidence("docs-lint", "skipped", "no_matching_paths"),
        )
    )
    first = asyncio.run(operations.record_verification(request, evidence))
    assert asyncio.run(operations.record_verification(request, evidence)) == first
    with connection_factory() as connection:
        rows = connection.execute(
            "SELECT reference, ciphertext, plaintext_bytes FROM verification_artifacts ORDER BY check_name"
        ).fetchall()
        receipt = connection.execute(
            "SELECT event_data FROM run_events WHERE event_key = %s", (request.idempotency_key,)
        ).fetchone()[0]
    assert len(rows) == 2
    assert "secret-value" not in json.dumps(receipt)
    assert "stdout" not in json.dumps(receipt)
    assert {item["evidence_ref"] for item in receipt["checks"]} == {row[0] for row in rows}
    assert all(row[2] <= 4096 for row in rows)
    payloads = [settings.decrypt(bytes(row[1])) for row in rows]
    failed = next(payload for payload in payloads if payload["status"] == "failed")
    assert failed["stdout"].startswith("[redacted]")
    assert failed["stdout"].endswith("[truncated]")
    assert failed["stderr"] == "[redacted] stderr"
    assert failed["report_summaries"][0]["failed"] == 1
    skipped = next(payload for payload in payloads if payload["status"] == "skipped")
    assert skipped["stdout"] == skipped["stderr"] == ""
    assert skipped["timed_out"] is False
    assert skipped["output_limit_exceeded"] is False


@pytest.mark.parametrize(
    ("timed_out", "output_limit_exceeded", "report_error", "error_code"),
    [
        (True, False, None, None),
        (False, True, None, None),
        (False, False, "invalid_report", "invalid_report"),
    ],
    ids=["timeout-without-overflow", "output-overflow", "report-validation-error"],
)
def test_verification_artifacts_preserve_runtime_facts_and_redact_cutoff_secrets(
    connection_factory, timed_out, output_limit_exceeded, report_error, error_code
):
    head = "b" * 40
    _seed(connection_factory, "a" * 40, head)
    with connection_factory() as connection:
        connection.execute("UPDATE runs SET state = 'verifying' WHERE public_id = %s", (RUN_ID,))
    secret = "synthetic-runtime-secret-value"
    prefix = secret[:16]
    settings = EvidenceSettings(Fernet.generate_key(), max_bytes=4096, secret_patterns=(secret,))
    sequence = iter(range(100, 200))
    operations = ProductionOperations(
        connection_factory,
        None,
        None,
        Path("."),
        _check_policy(),
        lambda: f"01J{next(sequence):023d}",
        settings,
    )
    request = StageRequest(
        owner_id=OWNER_ID, run_id=RUN_ID, head_sha=head, idempotency_key=f"{RUN_ID}:{head}:verify"
    )
    cutoff = timed_out or output_limit_exceeded
    sandbox = SandboxResult(
        exit_code=None if cutoff else 1,
        stdout="stdout " + (prefix if cutoff else secret),
        stderr="stderr " + (prefix if cutoff else secret),
        duration_ms=25,
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
        report_summaries=()
        if cutoff
        else ({"passed": 1, "failed": 0, "skipped": 0, "duration_ms": 25},),
        report_error=report_error,
    )
    evidence = VerificationEvidence(
        checks=(
            VerificationCheckEvidence("python-tests", "failed", "path_match", sandbox, error_code),
        )
    )
    first = asyncio.run(operations.record_verification(request, evidence))
    assert asyncio.run(operations.record_verification(request, evidence)) == first
    with connection_factory() as connection:
        rows = connection.execute(
            "SELECT ciphertext, plaintext_bytes FROM verification_artifacts"
        ).fetchall()
        receipt = connection.execute(
            "SELECT event_data FROM run_events WHERE event_key = %s", (request.idempotency_key,)
        ).fetchone()[0]
    assert len(rows) == 1
    ciphertext, plaintext_bytes = bytes(rows[0][0]), rows[0][1]
    assert plaintext_bytes <= settings.max_bytes
    assert prefix.encode() not in ciphertext
    # Inspect authenticated plaintext directly, not only the display-time redactor.
    encrypted_payload = json.loads(Fernet(settings.key).decrypt(ciphertext))
    payload = settings.decrypt(ciphertext)
    assert payload == encrypted_payload
    assert prefix not in json.dumps(encrypted_payload)
    suffix = "\n[truncated]" if output_limit_exceeded else ""
    assert payload["stdout"] == "stdout [redacted]" + suffix
    assert payload["stderr"] == "stderr [redacted]" + suffix
    for name in ("timed_out", "output_limit_exceeded", "exit_code", "duration_ms", "error_code"):
        expected = error_code if name == "error_code" else getattr(sandbox, name)
        assert payload[name] == receipt["checks"][0][name] == expected
    assert payload["report_error"] == report_error
    assert payload["report_summaries"] == list(sandbox.report_summaries)
    assert payload["check_name"] == "python-tests"
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "path_match"

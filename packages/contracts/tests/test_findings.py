import pytest
from pr_reliability_contracts import Evidence, EvidenceKind, Finding, FindingSeverity
from pydantic import ValidationError


def test_finding_round_trips_with_source_and_test_evidence(run_identity: dict) -> None:
    finding = Finding(
        **run_identity,
        category="correctness",
        severity=FindingSeverity.HIGH,
        claim="Retry path can charge twice.",
        confidence=0.94,
        evidence=(
            Evidence(
                schema_version="1",
                kind=EvidenceKind.SOURCE_LOCATION,
                summary="Charge happens before idempotency lookup.",
                file_path="src/checkout.py",
                start_line=91,
                end_line=95,
            ),
            Evidence(
                schema_version="1",
                kind=EvidenceKind.TEST_RESULT,
                summary="Retry test reproduces a second charge.",
                command=("pytest", "tests/test_checkout.py"),
                exit_code=1,
            ),
        ),
    )

    assert Finding.model_validate_json(finding.model_dump_json()) == finding


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "schema_version": "1",
                "kind": EvidenceKind.SOURCE_LOCATION,
                "summary": "missing location",
            },
            "requires file_path",
        ),
        (
            {
                "schema_version": "1",
                "kind": EvidenceKind.TEST_RESULT,
                "summary": "missing command",
                "exit_code": 1,
            },
            "requires command",
        ),
        (
            {
                "schema_version": "1",
                "kind": EvidenceKind.SOURCE_LOCATION,
                "summary": "backwards lines",
                "file_path": "app.py",
                "start_line": 10,
                "end_line": 5,
            },
            "must not be before",
        ),
    ],
)
def test_invalid_evidence_is_rejected(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Evidence.model_validate(payload)


def test_finding_requires_evidence_and_bounded_confidence(run_identity: dict) -> None:
    with pytest.raises(ValidationError) as error:
        Finding(
            **run_identity,
            category="correctness",
            severity=FindingSeverity.HIGH,
            claim="Unproven claim",
            confidence=1.1,
            evidence=(),
        )

    assert error.value.error_count() == 2


def test_evidence_requires_explicit_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        Evidence(
            kind=EvidenceKind.SOURCE_LOCATION,
            summary="Location without a wire version.",
            file_path="app.py",
        )

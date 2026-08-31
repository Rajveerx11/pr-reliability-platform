from itertools import pairwise

import pytest
from conftest import BASE_SHA, PULL_REQUEST_ID, REPOSITORY_ID, RUN_ID
from pr_reliability_contracts import (
    TERMINAL_RUN_STATES,
    CancelRunCommand,
    RunState,
    StartRunCommand,
    can_transition,
    require_transition,
)
from pydantic import ValidationError


def test_start_run_round_trips_and_keeps_identity(run_identity: dict) -> None:
    run_identity["schema_version"] = "1.1"
    command = StartRunCommand(
        **run_identity,
        generation=1,
        repository_id=REPOSITORY_ID,
        pull_request_id=PULL_REQUEST_ID,
        pull_request_number=17,
        base_sha=BASE_SHA,
        token_budget=12_000,
        cost_budget_usd_micros=500_000,
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    )

    restored = StartRunCommand.model_validate_json(command.model_dump_json())

    assert restored == command
    assert restored.schema_version == "1.1"
    assert restored.run_id == RUN_ID
    assert restored.traceparent == "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def test_start_run_rejects_invalid_traceparent(run_identity: dict) -> None:
    run_identity["schema_version"] = "1.1"
    with pytest.raises(ValidationError, match="traceparent"):
        StartRunCommand(
            **run_identity,
            generation=1,
            repository_id=REPOSITORY_ID,
            pull_request_id=PULL_REQUEST_ID,
            pull_request_number=17,
            base_sha=BASE_SHA,
            token_budget=12_000,
            cost_budget_usd_micros=500_000,
            traceparent="secret or malformed context",
        )


def test_start_run_parses_legacy_version_without_traceparent(run_identity: dict) -> None:
    command = StartRunCommand(
        **run_identity,
        generation=1,
        repository_id=REPOSITORY_ID,
        pull_request_id=PULL_REQUEST_ID,
        pull_request_number=17,
        base_sha=BASE_SHA,
        token_budget=12_000,
        cost_budget_usd_micros=500_000,
    )

    assert command.schema_version == "1"
    assert command.traceparent is None


def test_legacy_version_rejects_new_traceparent_field(run_identity: dict) -> None:
    with pytest.raises(ValidationError, match="requires schema version 1.1"):
        StartRunCommand(
            **run_identity,
            generation=1,
            repository_id=REPOSITORY_ID,
            pull_request_id=PULL_REQUEST_ID,
            pull_request_number=17,
            base_sha=BASE_SHA,
            token_budget=12_000,
            cost_budget_usd_micros=500_000,
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        )


@pytest.mark.parametrize("field", ["public_id", "owner_id", "run_id", "head_sha"])
def test_start_run_requires_identity(run_identity: dict, field: str) -> None:
    del run_identity[field]

    with pytest.raises(ValidationError):
        StartRunCommand(
            **run_identity,
            generation=1,
            repository_id=REPOSITORY_ID,
            pull_request_id=PULL_REQUEST_ID,
            pull_request_number=17,
            base_sha=BASE_SHA,
            token_budget=12_000,
            cost_budget_usd_micros=500_000,
        )


def test_unknown_input_is_rejected(run_identity: dict) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CancelRunCommand(**run_identity, reason="new commit", hidden_override=True)


def test_invalid_identifier_and_sha_are_rejected(run_identity: dict) -> None:
    run_identity["public_id"] = "81J00000000000000000000001"
    run_identity["head_sha"] = "B" * 40

    with pytest.raises(ValidationError) as error:
        CancelRunCommand(**run_identity, reason="new commit")

    assert error.value.error_count() == 2


def test_unknown_schema_version_is_rejected(run_identity: dict) -> None:
    run_identity["schema_version"] = "2"

    with pytest.raises(ValidationError, match="literal_error"):
        CancelRunCommand(**run_identity, reason="new commit")


def test_missing_schema_version_is_rejected(run_identity: dict) -> None:
    del run_identity["schema_version"]

    with pytest.raises(ValidationError, match="schema_version"):
        CancelRunCommand(**run_identity, reason="new commit")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pull_request_number", "17"),
        ("generation", "1"),
        ("pull_request_number", True),
        ("token_budget", "12000"),
        ("cost_budget_usd_micros", 1.5),
    ],
)
def test_start_run_rejects_coerced_json_types(
    run_identity: dict, field: str, value: object
) -> None:
    payload = {
        **run_identity,
        "generation": 1,
        "repository_id": REPOSITORY_ID,
        "pull_request_id": PULL_REQUEST_ID,
        "pull_request_number": 17,
        "base_sha": BASE_SHA,
        "token_budget": 12_000,
        "cost_budget_usd_micros": 500_000,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        StartRunCommand.model_validate(payload)


def test_state_machine_allows_expected_path() -> None:
    path = [
        RunState.QUEUED,
        RunState.SELECTING_CONTEXT,
        RunState.ANALYZING,
        RunState.VERIFYING,
        RunState.AWAITING_APPROVAL,
        RunState.PUBLISHED,
    ]

    assert all(can_transition(current, target) for current, target in pairwise(path))
    assert RunState.PUBLISHED in TERMINAL_RUN_STATES


def test_state_machine_rejects_skip_and_terminal_restart() -> None:
    with pytest.raises(ValueError, match="queued to published"):
        require_transition(RunState.QUEUED, RunState.PUBLISHED)

    assert not can_transition(RunState.FAILED, RunState.QUEUED)


@pytest.mark.parametrize("state", list(RunState))
def test_replayed_transition_to_persisted_state_is_idempotent(state: RunState) -> None:
    require_transition(state, state)


@pytest.mark.parametrize(
    "active_state",
    [
        RunState.QUEUED,
        RunState.SELECTING_CONTEXT,
        RunState.ANALYZING,
        RunState.VERIFYING,
        RunState.AWAITING_APPROVAL,
    ],
)
def test_every_active_state_can_fail_or_cancel(active_state: RunState) -> None:
    assert can_transition(active_state, RunState.FAILED)
    assert can_transition(active_state, RunState.CANCELLED)

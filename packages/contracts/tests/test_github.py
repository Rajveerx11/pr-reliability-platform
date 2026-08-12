import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from conftest import BASE_SHA, HEAD_SHA, OWNER_ID, PUBLIC_ID
from pr_reliability_contracts import PullRequestAction, PullRequestWebhook
from pydantic import ValidationError


def valid_webhook() -> PullRequestWebhook:
    return PullRequestWebhook(
        schema_version="1",
        public_id=PUBLIC_ID,
        owner_id=OWNER_ID,
        delivery_id="8b447be4-1234-4321-9876-129dfb1f0001",
        installation_id=42,
        repository_github_id=84,
        pull_request_number=17,
        action=PullRequestAction.SYNCHRONIZE,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        received_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    )


def test_webhook_round_trip_normalizes_time_to_utc() -> None:
    webhook = valid_webhook()

    assert webhook.received_at == datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
    assert PullRequestWebhook.model_validate_json(webhook.model_dump_json()) == webhook


def test_webhook_accepts_a_normal_decoded_json_dictionary() -> None:
    webhook = valid_webhook()
    decoded_json = json.loads(webhook.model_dump_json())

    assert PullRequestWebhook.model_validate(decoded_json) == webhook


def test_webhook_rejects_unknown_action_and_field() -> None:
    payload = valid_webhook().model_dump(mode="json")
    payload["action"] = "edited"
    payload["raw_payload"] = {"secret": "must not persist"}

    with pytest.raises(ValidationError) as error:
        PullRequestWebhook.model_validate_json(json.dumps(payload))

    assert error.value.error_count() == 2


def test_webhook_requires_explicit_schema_version() -> None:
    payload = valid_webhook().model_dump(mode="json")
    del payload["schema_version"]

    with pytest.raises(ValidationError, match="schema_version"):
        PullRequestWebhook.model_validate_json(json.dumps(payload))

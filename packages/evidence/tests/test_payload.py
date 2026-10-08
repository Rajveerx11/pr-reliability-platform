"""Bounds and redaction are enforced before persistence and after decryption."""

import json

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pr_reliability_evidence import EvidenceSettings, from_environment
from pr_reliability_evidence.payload import TRUNCATION_MARKER
from pr_reliability_evidence.store import evidence_reference


def settings(**kwargs):
    return EvidenceSettings(Fernet.generate_key(), **kwargs)


def test_ciphertext_redacts_before_truncating_and_is_authenticated():
    config = settings(max_bytes=4096, secret_patterns=("token-value",))
    payload = {
        "stdout": "x" * 250 + "token-value" + "😀" * 2000,
        "stderr": "token-value\n",
        "output_limit_exceeded": False,
    }
    encrypted, size = config.encrypt(payload)
    assert b"token-value" not in encrypted
    assert size <= config.max_bytes
    result = config.decrypt(encrypted)
    assert "token-value" not in json.dumps(result)
    assert result["stdout"].endswith(TRUNCATION_MARKER)
    assert result["stderr"] == "[redacted]\n"
    with pytest.raises(InvalidToken):
        settings().decrypt(encrypted)


def test_runtime_truncation_is_marked_even_if_text_is_short():
    config = settings()
    encrypted, _ = config.encrypt({"stdout": "ok", "stderr": "", "output_limit_exceeded": True})
    assert config.decrypt(encrypted)["stdout"] == "ok" + TRUNCATION_MARKER
    assert config.decrypt(encrypted)["stderr"] == TRUNCATION_MARKER


def test_current_redaction_rules_apply_to_display():
    key = Fernet.generate_key()
    old = EvidenceSettings(key)
    encrypted, _ = old.encrypt({"stdout": "new-secret", "stderr": ""})
    current = EvidenceSettings(key, secret_patterns=("new-secret",))
    assert current.decrypt(encrypted)["stdout"] == "[redacted]"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": 10485761},
        {"retention_seconds": 0},
        {"retention_seconds": 90 * 86400 + 1},
        {"secret_patterns": ("",)},
        {"secret_patterns": (5,)},
    ],
)
def test_invalid_settings_fail_closed(kwargs):
    with pytest.raises(ValueError):
        settings(**kwargs)


def test_environment_requires_key_and_literal_pattern_list():
    with pytest.raises(RuntimeError):
        from_environment({})
    with pytest.raises(TypeError):
        from_environment(
            {
                "EVIDENCE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "EVIDENCE_SECRET_PATTERNS": "{}",
            }
        )


def test_references_are_opaque_stable_and_owner_run_scoped():
    assert evidence_reference("owner", "run", "unit") == evidence_reference("owner", "run", "unit")
    assert (
        len({evidence_reference(owner, run, "unit") for owner in ("a", "b") for run in ("1", "2")})
        == 4
    )
    assert "owner" not in evidence_reference("owner", "run", "unit")

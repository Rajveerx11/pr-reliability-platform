"""Redact then bound log text before authenticated encryption."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from cryptography.fernet import Fernet

TRUNCATION_MARKER = "\n[truncated]"


@dataclass(frozen=True)
class EvidenceSettings:
    key: bytes
    max_bytes: int = 256 * 1024
    retention_seconds: int = 7 * 86400
    secret_patterns: tuple[str, ...] = ()

    def __post_init__(self):
        Fernet(self.key)
        if type(self.max_bytes) is not int or not 4096 <= self.max_bytes <= 10 * 1024 * 1024:
            raise ValueError("evidence max_bytes must be between 4096 and 10485760")
        if (
            type(self.retention_seconds) is not int
            or not 60 <= self.retention_seconds <= 90 * 86400
        ):
            raise ValueError("evidence retention must be between 60 seconds and 90 days")
        # Literal secret patterns avoid regex backtracking on hostile output.
        if len(self.secret_patterns) > 128 or any(
            not isinstance(p, str) or not p or len(p) > 4096 for p in self.secret_patterns
        ):
            raise ValueError("invalid evidence secret patterns")

    @property
    def retention(self):
        return timedelta(seconds=self.retention_seconds)

    def redact(self, text: str) -> str:
        for pattern in sorted(self.secret_patterns, key=len, reverse=True):
            text = text.replace(pattern, "[redacted]")
        return text

    def encrypt(self, payload: dict) -> tuple[bytes, int]:
        safe = dict(payload)
        # Allow ample bounded space for summaries, escaping and JSON structure.
        budget = self.max_bytes // 16
        for name in ("stdout", "stderr"):
            text = self.redact(str(safe.get(name, "")))
            raw = text.encode("utf-8")
            if len(raw) > budget or safe.get("output_limit_exceeded"):
                text = raw[:budget].decode("utf-8", errors="ignore") + TRUNCATION_MARKER
            safe[name] = text
        raw = json.dumps(safe, ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > self.max_bytes:
            raise ValueError("evidence payload exceeds configured size")
        return Fernet(self.key).encrypt(raw), len(raw)

    def decrypt(self, ciphertext: bytes) -> dict:
        if len(ciphertext) > (self.max_bytes * 2 + 1024):
            raise ValueError("evidence ciphertext exceeds configured size")
        raw = Fernet(self.key).decrypt(ciphertext)
        if len(raw) > self.max_bytes:
            raise ValueError("evidence plaintext exceeds configured size")
        payload = json.loads(raw)
        # Reapply current operator redaction rules before display/download.
        for name in ("stdout", "stderr"):
            payload[name] = self.redact(payload[name])
        return payload


def from_environment(env: Mapping[str, str]) -> EvidenceSettings:
    key = env.get("EVIDENCE_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("EVIDENCE_ENCRYPTION_KEY is required")
    patterns = json.loads(env.get("EVIDENCE_SECRET_PATTERNS", "[]"))
    if not isinstance(patterns, list):
        raise TypeError("EVIDENCE_SECRET_PATTERNS must be a JSON list of literal strings")
    return EvidenceSettings(
        key=key.encode("ascii"),
        max_bytes=int(env.get("EVIDENCE_MAX_BYTES", "262144")),
        retention_seconds=int(env.get("EVIDENCE_RETENTION_SECONDS", "604800")),
        secret_patterns=tuple(patterns),
    )

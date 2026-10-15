"""Fixed operational alerts to an explicitly configured private HTTPS receiver."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
from cryptography import x509
from pr_reliability_api.operations.store import OperationsStore
from pydantic import BaseModel, ConfigDict, Field, field_validator

_CODES = frozenset(
    {
        "alert_check_unavailable",
        "queue_probe_unavailable",
        "missing_workers",
        "stuck_queue",
        "repeated_failure",
        "low_disk",
        "failed_backups",
        "expiring_tls",
        "disk_probe_unavailable",
        "backup_probe_unavailable",
        "tls_probe_unavailable",
    }
)
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


class AlertSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # This field must be set by an operator after approval; no public/default receiver exists.
    approved_receiver_url: str
    queue: str = Field(default="pr-review", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    disk_path: Path
    backup_receipt: Path
    tls_certificate: Path
    stuck_seconds: int = Field(default=300, ge=30, le=86400)
    backup_max_age_seconds: int = Field(default=172800, ge=60, le=604800)
    tls_warning_days: int = Field(default=14, ge=1, le=90)

    @field_validator("approved_receiver_url")
    @classmethod
    def private_receiver(cls, value):
        parsed = urlsplit(value)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            raise ValueError("receiver must use a private literal IP, not DNS") from None
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or not any(address in network for network in _PRIVATE_NETWORKS)
        ):
            raise ValueError("receiver must be private HTTPS without credentials or query")
        return value


def probe_host(settings: AlertSettings, now: datetime | None = None) -> set[str]:
    import shutil

    now = now or datetime.now(UTC)
    alerts = set()
    try:
        disk = shutil.disk_usage(settings.disk_path)
        if disk.free / disk.total < 0.10:
            alerts.add("low_disk")
    except (OSError, ZeroDivisionError):
        alerts.add("disk_probe_unavailable")
    try:
        with settings.backup_receipt.open("rb") as receipt:
            raw = receipt.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized receipt")
        data = json.loads(raw)
        finished = datetime.fromisoformat(data["finished_at"])
        if finished.tzinfo is None or finished > now or type(data["succeeded"]) is not bool:
            raise ValueError("invalid receipt")
        if not data["succeeded"] or now - finished > timedelta(
            seconds=settings.backup_max_age_seconds
        ):
            alerts.add("failed_backups")
    except (OSError, ValueError, KeyError, TypeError):
        alerts.add("backup_probe_unavailable")
    try:
        with settings.tls_certificate.open("rb") as certificate:
            raw = certificate.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized certificate")
        expiry = x509.load_pem_x509_certificate(raw).not_valid_after_utc
        if expiry <= now + timedelta(days=settings.tls_warning_days):
            alerts.add("expiring_tls")
    except (OSError, ValueError):
        alerts.add("tls_probe_unavailable")
    return alerts


def evaluate_alerts(snapshot: dict, stuck_seconds: int = 300) -> set[str]:
    codes = set()
    if snapshot.get("queue_observation_unknown", 0):
        codes.add("queue_probe_unavailable")
    available = {r["workload"] for r in snapshot["runners"] if r["state"] in {"online", "busy"}}
    if not {"workflow", "review"} <= available:
        codes.add("missing_workers")
    if snapshot["queue_depth"] and (snapshot["current_wait_seconds"] or 0) >= stuck_seconds:
        codes.add("stuck_queue")
    if snapshot["recent_failures"] >= 3 and (
        snapshot["recent_failures"] / max(1, snapshot["recent_completed"]) >= 0.5
    ):
        codes.add("repeated_failure")
    return codes


def deliver_alerts(settings: AlertSettings, codes: set[str], token: str, *, client=None) -> None:
    """Never forward arbitrary labels, annotations, host names, source, or exception text."""
    if not codes <= _CODES or not token or "\n" in token or "\r" in token:
        raise ValueError("invalid alert code or receiver authentication")
    if not codes:
        return
    payload = {
        "schema_version": "1",
        "service": "pr-reliability",
        "alerts": [{"code": code, "severity": "warning"} for code in sorted(codes)],
    }
    if client is None:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as private_client:
            return deliver_alerts(settings, codes, token, client=private_client)
    response = client.post(
        settings.approved_receiver_url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    # httpx status exceptions contain receiver URL; report a fixed safe code instead.
    if not 200 <= response.status_code < 300:
        raise RuntimeError("private alert receiver rejected delivery")


def main():
    """Supported Compose runs the periodic monitor; --once supports a local check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        settings = AlertSettings.model_validate_json(args.config.read_bytes())
        database_url = os.environ["DATABASE_URL"]
        owner_id = os.environ["OWNER_ID"]
        token = Path(os.environ["OPERATIONS_ALERT_TOKEN_FILE"]).read_text().strip()
        store = OperationsStore(
            lambda: psycopg.connect(
                database_url, connect_timeout=5, options="-c statement_timeout=5000"
            )
        )
        from .alert_monitor import CheckHealth, check_once, run_monitor, serve_health

        health = CheckHealth()
        if args.once:
            if not check_once(store, owner_id, settings, token, health):
                raise RuntimeError("operational alert check unavailable")
        else:
            server = serve_health(health)
            try:
                run_monitor(store, owner_id, settings, token, health=health)
            finally:
                server.shutdown()
    except (OSError, ValueError, KeyError, psycopg.Error, httpx.HTTPError, RuntimeError):
        # Do not print validation errors (they may contain operator inputs) or HTTP exceptions.
        raise SystemExit("operational alert check unavailable") from None


if __name__ == "__main__":
    main()

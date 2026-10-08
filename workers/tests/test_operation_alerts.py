"""Alert receiver privacy, fixed metadata, and real local probe failure paths."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pr_reliability_workers.operation_alerts import (
    AlertSettings,
    deliver_alerts,
    evaluate_alerts,
    probe_host,
)


def settings(tmp_path, **kwargs):
    return AlertSettings(
        approved_receiver_url="https://10.0.0.5/alerts",
        disk_path=tmp_path,
        backup_receipt=tmp_path / "backup.json",
        tls_certificate=tmp_path / "cert.pem",
        **kwargs,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/alerts",
        "https://example.com/alerts",
        "https://127.0.0.1/alerts",
        "https://169.254.169.254/",
        "https://8.8.8.8/alerts",
        "https://user:secret@10.0.0.5/",
        "https://10.0.0.5/?secret=x",
        "https://10.0.0.5/#source",
        "https://10.0.0.5:8080/",
    ],
)
def test_rejects_public_metadata_dns_and_credential_receivers(tmp_path, url):
    config = settings(tmp_path).model_dump() | {"approved_receiver_url": url}
    with pytest.raises(ValueError):
        AlertSettings.model_validate(config)


def test_fixed_payload_no_queue_runner_path_source_or_ids(tmp_path):
    captured = []

    def receive(request):
        captured.append(request)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        deliver_alerts(
            settings(tmp_path), {"missing_workers", "stuck_queue"}, "test-only", client=client
        )
    payload = json.loads(captured[0].content)
    assert payload == {
        "schema_version": "1",
        "service": "pr-reliability",
        "alerts": [
            {"code": "missing_workers", "severity": "warning"},
            {"code": "stuck_queue", "severity": "warning"},
        ],
    }
    assert captured[0].headers["authorization"] == "Bearer test-only"
    assert "test-only" not in captured[0].content.decode()
    with pytest.raises(ValueError):
        deliver_alerts(settings(tmp_path), {"raw-private-source"}, "test-only")
    with pytest.raises(ValueError):
        deliver_alerts(settings(tmp_path), {"stuck_queue"}, "")


def test_receiver_redirect_never_follows_or_leaks_auth(tmp_path):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://public.example/steal"})

    with (
        httpx.Client(transport=httpx.MockTransport(receive)) as client,
        pytest.raises(RuntimeError, match="private alert receiver rejected"),
    ):
        deliver_alerts(settings(tmp_path), {"stuck_queue"}, "test-only", client=client)
    assert len(requests) == 1


def test_missing_draining_workers_stuck_queue_repeated_failures():
    snapshot = {
        "runners": [],
        "queue_depth": 3,
        "current_wait_seconds": 301,
        "recent_failures": 3,
        "recent_completed": 4,
    }
    assert evaluate_alerts(snapshot) == {"missing_workers", "stuck_queue", "repeated_failure"}
    snapshot["runners"] = [
        {"workload": "review", "state": "draining"},
        {"workload": "workflow", "state": "online"},
    ]
    assert "missing_workers" in evaluate_alerts(snapshot)
    snapshot["runners"][0]["state"] = "online"
    snapshot.update(current_wait_seconds=None, recent_failures=0)
    assert evaluate_alerts(snapshot) == set()


def certificate(path, expiry, now):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-private-certificate")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(expiry)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_low_disk_failed_backup_expiring_tls_probes(tmp_path, monkeypatch):
    from collections import namedtuple

    disk = namedtuple("Disk", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _: disk(100, 95, 5))
    config = settings(tmp_path)
    now = datetime.now(UTC)
    config.backup_receipt.write_text(
        json.dumps({"succeeded": False, "finished_at": now.isoformat()})
    )
    certificate(config.tls_certificate, now + timedelta(days=2), now)
    assert probe_host(config, now) == {"low_disk", "failed_backups", "expiring_tls"}
    monkeypatch.setattr("shutil.disk_usage", lambda _: disk(100, 10, 90))
    config.backup_receipt.write_text(
        json.dumps({"succeeded": True, "finished_at": now.isoformat()})
    )
    certificate(config.tls_certificate, now + timedelta(days=90), now)
    assert probe_host(config, now) == set()
    config.backup_receipt.write_text(
        json.dumps({"succeeded": True, "finished_at": (now - timedelta(days=3)).isoformat()})
    )
    assert probe_host(config, now) == {"failed_backups"}


def test_probe_unknown_is_not_healthy_and_does_not_reflect_local_data(tmp_path, monkeypatch):
    def failed(_):
        raise OSError("private-secret-path")

    monkeypatch.setattr("shutil.disk_usage", failed)
    config = settings(tmp_path)
    assert probe_host(config) == {
        "disk_probe_unavailable",
        "backup_probe_unavailable",
        "tls_probe_unavailable",
    }
    config.backup_receipt.write_text("sensitive source" * 500)
    config.tls_certificate.write_text("invalid private data")
    assert probe_host(config) == {
        "disk_probe_unavailable",
        "backup_probe_unavailable",
        "tls_probe_unavailable",
    }

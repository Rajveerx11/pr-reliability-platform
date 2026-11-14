"""Published detector results must not imply package tests were executed."""

import json
from types import SimpleNamespace

import pytest
from pr_reliability_proof_adapter import _published_gate


@pytest.mark.parametrize("passed", [True, False])
def test_published_gate_is_detector_only(monkeypatch, capsys, tmp_path, passed):
    import proofofwork.engine

    calls = []

    def check(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(passed=passed, reasons=[], findings=[])

    monkeypatch.setattr(proofofwork.engine, "check", check)
    monkeypatch.setattr(_published_gate, "_validate_changeset", lambda *_: None)
    monkeypatch.setattr(_published_gate.sys, "argv", ["gate", str(tmp_path), "HEAD", "test.log"])
    _published_gate.main()
    assert calls[0]["run_tests"] is False
    assert calls[0]["run_mutation"] is False
    assert calls[0]["use_judge"] is False
    assert json.loads(capsys.readouterr().out)["payload"]["passed"] is passed

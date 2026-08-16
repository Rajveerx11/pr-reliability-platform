"""Tests for honest full-cohort evaluation replay reporting."""

import json
import shutil
from pathlib import Path

import pytest

from evals.evaluation_models import ReplayFinding, ReplayManifest
from evals.evaluation_runner import EvaluationError, main, run_evaluation
from evals.golden_prs.corpus import CORPUS_ROOT, corpus_fingerprint, load_corpus

REPLAY = Path(__file__).parents[1] / "replays" / "full_cohort_harness.json"


def manifest() -> ReplayManifest:
    return ReplayManifest.model_validate_json(REPLAY.read_text(encoding="utf-8"))


def test_harness_replay_reports_full_cohort_without_inventing_measurements() -> None:
    report = run_evaluation(manifest())

    assert report["run"]["mode"] == "deterministic_replay"
    assert report["run"]["provider"] is None
    assert report["run"]["model"] is None
    assert report["cohort"] == {"reported_tasks": 10, "total_tasks": 10}
    assert report["quality"]["defect_recall"] is None
    assert report["quality"]["defect_recall_status"] == "unknown_no_model_attempts"
    assert report["quality"]["reported_finding_false_positive_rate"] is None
    assert report["quality"]["false_positive_rate_status"] == ("unknown_no_reported_findings")
    assert report["performance"]["attempted_tasks"] == 0
    assert report["performance"]["run_success_rate"] is None
    assert report["performance"]["p50_latency_ms"] is None
    assert report["performance"]["p95_latency_ms"] is None
    assert report["usage"]["coverage_rate"] == 0
    assert report["usage"]["total_tokens"] is None
    assert report["usage"]["reported_cost_usd_micros"] is None
    assert all(result["protected_verifier"]["broken_rejected"] for result in report["task_results"])
    assert all(
        result["protected_verifier"]["reference_fix_accepted"] for result in report["task_results"]
    )


def test_replay_must_include_each_frozen_task_once() -> None:
    incomplete = manifest().model_copy(update={"attempts": manifest().attempts[:-1]})

    with pytest.raises(EvaluationError, match="full cohort"):
        run_evaluation(incomplete)


def test_scoring_counts_adjudicated_matches_and_false_findings() -> None:
    original = manifest()
    first = original.attempts[0].model_copy(
        update={
            "status": "completed",
            "findings": (
                ReplayFinding(summary="matches seeded defect", matched_defect_index=0),
                ReplayFinding(summary="unmatched finding"),
            ),
        }
    )
    scored = original.model_copy(update={"attempts": (first, *original.attempts[1:])})

    report = run_evaluation(scored)

    assert report["quality"]["defect_recall"] == 1
    assert report["quality"]["reported_finding_false_positive_rate"] == 0.5
    assert report["performance"]["run_success_rate"] == 1


def test_cli_writes_machine_and_human_reports(tmp_path: Path) -> None:
    json_output = tmp_path / "report.json"
    markdown_output = tmp_path / "report.md"

    exit_code = main(
        [
            "--input",
            str(REPLAY),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert exit_code == 0
    assert json.loads(json_output.read_text(encoding="utf-8"))["cohort"]["total_tasks"] == 10
    markdown = markdown_output.read_text(encoding="utf-8")
    assert "Not a real model run" in markdown
    assert "Full cohort: 10/10" in markdown


def test_corpus_fingerprint_is_stable_across_crlf_checkout(tmp_path: Path) -> None:
    copied = tmp_path / "golden_prs"
    shutil.copytree(CORPUS_ROOT, copied)
    for path in copied.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".py"}:
            content = path.read_bytes().replace(b"\r\n", b"\n")
            path.write_bytes(content.replace(b"\n", b"\r\n"))

    assert corpus_fingerprint(load_corpus(copied / "tasks")) == corpus_fingerprint(load_corpus())

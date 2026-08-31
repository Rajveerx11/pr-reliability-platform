"""Run and score a reproducible full-cohort evaluation replay."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from evals.evaluation_models import ReplayManifest
from evals.evaluation_report import render_markdown
from evals.golden_prs.corpus import corpus_fingerprint, load_corpus, verify_task

RUNNER_VERSION = "1"


class EvaluationError(ValueError):
    """Replay input or corpus failed evaluation invariants."""


def run_evaluation(manifest: ReplayManifest) -> dict[str, Any]:
    """Verify complete corpus, score recorded findings, preserve unknown measurements."""

    tasks = load_corpus()
    fingerprint = corpus_fingerprint(tasks)
    if manifest.corpus_fingerprint != fingerprint:
        raise EvaluationError("replay corpus fingerprint does not match loaded corpus")

    attempts = {attempt.task_id: attempt for attempt in manifest.attempts}
    task_ids = {task.id for task in tasks}
    if set(attempts) != task_ids:
        missing = sorted(task_ids - set(attempts))
        extra = sorted(set(attempts) - task_ids)
        raise EvaluationError(f"replay must contain full cohort; missing={missing}, extra={extra}")

    task_results = []
    true_positives = false_positives = 0
    total_known_defects = sum(len(task.known_defects) for task in tasks)
    for task in tasks:
        attempt = attempts[task.id]
        broken = verify_task(
            task, fixed=False, timeout_seconds=manifest.limits.verifier_timeout_seconds
        )
        fixed = verify_task(
            task, fixed=True, timeout_seconds=manifest.limits.verifier_timeout_seconds
        )
        if broken.returncode == 0 or fixed.returncode != 0:
            raise EvaluationError(f"protected verifier invariant failed for {task.id}")

        if attempt.status == "not_run":
            task_results.append(
                {
                    "task_id": task.id,
                    "category": task.category,
                    "difficulty": task.difficulty,
                    "status": attempt.status,
                    "known_defect_count": len(task.known_defects),
                    "true_positive_count": None,
                    "false_positive_count": None,
                    "false_negative_count": None,
                    "protected_verifier": {
                        "broken_rejected": True,
                        "reference_fix_accepted": True,
                    },
                    "agent_duration_ms": None,
                    "end_to_end_latency_ms": None,
                    "usage": attempt.usage.model_dump(mode="json"),
                    "retries": 0,
                }
            )
            continue

        matched = []
        task_false_positives = 0
        for finding in attempt.findings:
            if finding.matched_defect_index is None:
                task_false_positives += 1
                continue
            if finding.matched_defect_index >= len(task.known_defects):
                raise EvaluationError(f"finding defect index out of range for {task.id}")
            matched.append(finding.matched_defect_index)
        if len(matched) != len(set(matched)):
            raise EvaluationError(f"more than one finding matches same defect for {task.id}")

        task_true_positives = len(matched)
        task_false_negatives = len(task.known_defects) - task_true_positives
        true_positives += task_true_positives
        false_positives += task_false_positives
        task_results.append(
            {
                "task_id": task.id,
                "category": task.category,
                "difficulty": task.difficulty,
                "status": attempt.status,
                "known_defect_count": len(task.known_defects),
                "true_positive_count": task_true_positives,
                "false_positive_count": task_false_positives,
                "false_negative_count": task_false_negatives,
                "protected_verifier": {"broken_rejected": True, "reference_fix_accepted": True},
                "agent_duration_ms": attempt.agent_duration_ms,
                "end_to_end_latency_ms": attempt.end_to_end_latency_ms,
                "usage": attempt.usage.model_dump(mode="json"),
                "retries": attempt.retries,
            }
        )

    reported_findings = true_positives + false_positives
    latencies = [
        attempt.end_to_end_latency_ms
        for attempt in manifest.attempts
        if attempt.end_to_end_latency_ms is not None
    ]
    durations = [
        attempt.agent_duration_ms
        for attempt in manifest.attempts
        if attempt.agent_duration_ms is not None
    ]
    known_usage = [attempt for attempt in manifest.attempts if attempt.usage.coverage != "unknown"]
    known_costs = [
        attempt.usage.reported_cost_usd_micros
        for attempt in manifest.attempts
        if attempt.usage.reported_cost_usd_micros is not None
    ]
    attempted = sum(attempt.status != "not_run" for attempt in manifest.attempts)
    successful = sum(attempt.status == "completed" for attempt in manifest.attempts)
    false_negatives = total_known_defects - true_positives if attempted else None
    if not attempted:
        recall = None
        recall_status = "unknown_no_model_attempts"
    else:
        recall = true_positives / total_known_defects if total_known_defects else None
        recall_status = (
            "known_full_cohort"
            if attempted == len(manifest.attempts)
            else "known_partial_cohort_not_run_counted_as_missed"
        )

    return {
        "schema_version": 1,
        "runner_version": RUNNER_VERSION,
        "run": {
            "mode": manifest.run_mode,
            "label": manifest.label,
            "recorded_at": manifest.recorded_at.isoformat(),
            "provider": manifest.provider,
            "model": manifest.model,
            "evaluated_commit": manifest.evaluated_commit,
            "corpus_fingerprint": fingerprint,
        },
        "environment": {
            "operating_system": platform.platform(),
            "python": platform.python_version(),
            "pydantic": version("pydantic"),
        },
        "limits": manifest.limits.model_dump(mode="json"),
        "cohort": {"reported_tasks": len(task_results), "total_tasks": len(tasks)},
        "quality": {
            "true_positive_count": true_positives,
            "false_positive_count": false_positives,
            "false_negative_count": false_negatives,
            "defect_recall": recall,
            "defect_recall_status": recall_status,
            "reported_finding_false_positive_rate": (
                false_positives / reported_findings if reported_findings else None
            ),
            "false_positive_rate_status": (
                "known" if reported_findings else "unknown_no_reported_findings"
            ),
        },
        "performance": {
            "attempted_tasks": attempted,
            "successful_attempts": successful,
            "not_run_tasks": len(manifest.attempts) - attempted,
            "run_success_rate": successful / attempted if attempted else None,
            "agent_duration_known_attempts": len(durations),
            "p50_agent_duration_ms": _percentile(durations, 50),
            "latency_known_attempts": len(latencies),
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
            "total_retries": sum(attempt.retries for attempt in manifest.attempts),
            "timeout_count": sum(attempt.status == "timeout" for attempt in manifest.attempts),
        },
        "usage": {
            "known_attempts": len(known_usage),
            "coverage_rate": len(known_usage) / len(manifest.attempts),
            "prompt_tokens": _sum_if_complete(manifest, "prompt_tokens"),
            "completion_tokens": _sum_if_complete(manifest, "completion_tokens"),
            "total_tokens": _sum_if_complete(manifest, "total_tokens"),
            "reported_cost_usd_micros": (
                sum(known_costs) if len(known_costs) == len(manifest.attempts) else None
            ),
            "cost_status": "known" if len(known_costs) == len(manifest.attempts) else "unknown",
        },
        "task_results": task_results,
        "limitations": list(manifest.limitations),
    }


def _percentile(values: list[int], percentile: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    return statistics.quantiles(values, n=100, method="inclusive")[percentile - 1]


def _sum_if_complete(manifest: ReplayManifest, field: str) -> int | None:
    values = [getattr(attempt.usage, field) for attempt in manifest.attempts]
    return sum(values) if all(value is not None for value in values) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = ReplayManifest.model_validate_json(args.input.read_text(encoding="utf-8"))
    report = run_evaluation(manifest)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

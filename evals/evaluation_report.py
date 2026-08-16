"""Human-readable rendering for version-one evaluation reports."""

from typing import Any


def render_markdown(report: dict[str, Any]) -> str:
    """Render report without turning unknown measurements into zero."""

    run = report["run"]
    quality = report["quality"]
    performance = report["performance"]
    usage = report["usage"]
    limits = report["limits"]
    lines = [
        "# Version-one evaluation report",
        "",
        "> Deterministic harness replay. Not a real model run and not single-agent quality evidence.",
        "",
        f"- Mode: `{run['mode']}`",
        f"- Label: {run['label']}",
        f"- Recorded: `{run['recorded_at']}`",
        f"- Evaluated commit: `{run['evaluated_commit']}`",
        f"- Corpus fingerprint: `{run['corpus_fingerprint']}`",
        "- Provider: unknown (no configured provider)",
        "- Model: unknown (no configured model)",
        (
            f"- Environment: `{report['environment']['operating_system']}`; Python "
            f"`{report['environment']['python']}`; Pydantic "
            f"`{report['environment']['pydantic']}`"
        ),
        "",
        "## Results",
        "",
        f"- Full cohort: {report['cohort']['reported_tasks']}/{report['cohort']['total_tasks']}",
        "- Defect recall: "
        + (
            f"{quality['defect_recall']:.1%} ({quality['true_positive_count']} true positives, "
            f"{quality['false_negative_count']} false negatives)"
            if quality["defect_recall"] is not None
            else "unknown (no model attempts ran)"
        ),
        (
            "- Reported-finding false-positive rate: "
            + _percentage(
                quality["reported_finding_false_positive_rate"],
                "unknown (no reported findings; denominator is zero)",
            )
        ),
        (
            "- Run success rate: "
            + _percentage(performance["run_success_rate"], "unknown (no model attempts ran)")
        ),
        (
            "- p50/p95 latency: "
            + _latency(performance["p50_latency_ms"], performance["p95_latency_ms"])
        ),
        f"- p50 agent duration: {_milliseconds(performance['p50_agent_duration_ms'])}",
        f"- Usage coverage: {usage['coverage_rate']:.1%}",
        "- Input/output/total tokens: unknown",
        "- Exact reported cost: unknown",
        f"- Retries: {performance['total_retries']}; timeouts: {performance['timeout_count']}",
        "",
        "## Limits",
        "",
        f"- Context token budget: {_known(limits['context_token_budget'])}",
        f"- Protected verifier timeout: {limits['verifier_timeout_seconds']} seconds per invocation",
        f"- Disposable sandbox enabled: {str(limits['sandbox_enabled']).lower()}",
        f"- Proof gate enabled: {str(limits['proof_gate_enabled']).lower()}",
        "",
        "## Full cohort",
        "",
        "| Task | Category | Status | TP | FP | FN | Broken rejected | Fix accepted |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in report["task_results"]:
        lines.append(
            f"| `{result['task_id']}` | {result['category']} | {result['status']} | "
            f"{_known(result['true_positive_count'])} | {_known(result['false_positive_count'])} | "
            f"{_known(result['false_negative_count'])} | yes | yes |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def _known(value: object) -> str:
    return str(value) if value is not None else "unknown"


def _percentage(value: float | None, unknown: str) -> str:
    return f"{value:.1%}" if value is not None else unknown


def _milliseconds(value: float | None) -> str:
    return f"{value:g} ms" if value is not None else "unknown"


def _latency(p50: float | None, p95: float | None) -> str:
    if p50 is None or p95 is None:
        return "unknown (replay contains no measured model latency)"
    return f"{p50:g} ms / {p95:g} ms"

# Evaluation

## Purpose

Measure whether the platform finds real pull request defects without producing too many false
alarms. Multi-agent work must beat the single-agent baseline on the same frozen tasks.

## Golden task shape

Each task contains:

- Stable task ID and corpus version
- Base repository fixture
- Pull request diff
- Known defects
- Allowed alternative findings
- Protected verifier
- Timeout and budget limits
- Category and difficulty

Version one contains ten frozen Python tasks in `evals/golden_prs/tasks/`. Each task keeps the
broken fixture, reference fix, protected verifier, known defect, and allowed finding together.
The verifier and shared verifier support live outside the agent workspace. Candidate modules run
in isolated child probes, so process exits cannot bypass parent assertions. Protected files are
checked before and after every verification run. A fail-closed tamper pass rejects direct process
exit and Python frame-inspection primitives before execution.

The corpus loader rejects unknown manifest fields, duplicate IDs, path escapes, symlinks, and
unprotected verifiers. It loads tasks in ID order. `corpus.sha256` freezes all metadata, fixture,
reference-fix, and verifier bytes so accidental changes are visible.

This runner is tamper-resistant evaluation for reviewed fixtures. It is not a security sandbox.
Untrusted pull request code must use the merged disposable sandbox described in
`security.md`.

## Proof of Work gate

The worker first runs repository tests in the disposable Docker sandbox. It then calls the
published `proof-of-work-agent` package through `pr_reliability_proof_adapter`. Package test
execution stays disabled because untrusted tests must never run on the activity-worker host.

The adapter returns version 1 of a small verdict: pass or fail, reasons, unique finding rule
IDs, and the installed package version. Application code imports only this local adapter, not
Proof of Work modules. Package errors, timeouts, and malformed results produce no verdict and
fail verification. A failed verdict is recorded as evidence but cannot reach human approval.

The published package runs in a dedicated child process. Before it starts, the adapter requires a
clean workspace whose `HEAD` equals the requested review head and whose base commit is its ancestor.
It clones that exact commit into a private local snapshot, so concurrent source changes cannot
change the inspected tree. On Linux, a durable subreaper supervisor starts the package as a child,
stays alive after hard package exits, and kills and reaps adopted descendants through a bounded
cleanup handshake. Windows uses a kill-on-close Job Object. Cleanup runs after success, failure,
malformed output, cancellation, or timeout. Production workers reject injected proof runners.
Output is bounded and package logs stay only in the private temporary directory. Cleanup failure
blocks verification, and the directory is removed after every outcome.

The ten tasks cover:

- Payment idempotency
- Organization authorization
- Empty pagination
- Retry limits
- GitHub webhook signatures
- Artifact path traversal
- Cache invalidation
- Money rounding
- Timezone preservation
- Webhook delivery deduplication

## Required metrics

- Defect recall
- False-positive rate
- Run success rate
- p50 and p95 latency
- Agent and verification duration
- Input and output tokens when reported
- Exact cost when reported
- Usage coverage
- Retry and timeout count

Missing usage stays unknown. It is never estimated or shown as zero.

Defect recall is unique matched known defects divided by all known defects. The
reported-finding false-positive rate is unmatched findings divided by all reported findings. If
there are no reported findings, the false-positive rate is unknown because its denominator is
zero. This definition measures false discoveries among findings; the frozen set has no clean
negative tasks for a task-level specificity measure.

If at least one model attempt runs but part of the cohort remains `not_run`, every known defect in
the frozen cohort stays in the recall denominator. This prevents partial runs from overstating
recall. A run with no model attempts keeps recall unknown. Only `completed` attempts may contribute
scored findings; failed and timed-out attempts retain performance facts but never partial quality
credit.

## Reproducible runner

`evals/evaluation_runner.py` loads one strict replay manifest, requires exactly the frozen ten-task
cohort, checks its fingerprint, and runs each protected verifier against both the broken fixture
and reference fix. It writes machine-readable JSON and a Markdown report under ignored
`artifacts/` paths:

```text
uv run python -m evals.evaluation_runner \
  --input evals/replays/full_cohort_harness.json \
  --json-output artifacts/evaluation/full_cohort_harness.json \
  --markdown-output artifacts/evaluation/full_cohort_harness.md
```

The committed replay is explicitly a deterministic harness replay. It contains no model output.
Provider, model, latency, duration, token usage, and cost remain unknown. See
`docs/evaluation-report.md` for its complete cohort and blocker report.
The harness, observability dependency, and Proof of Work adapter are now merged to `main`. The
committed replay still describes its historical evaluated commit and contains no model attempt;
merging the code does not turn that replay into a quality baseline.

Replay manifests record the evaluated commit, corpus fingerprint, run time, provider/model facts,
limits, every task attempt, adjudicated defect matches, retries, usage, and limitations. Unknown
measurements use `null`; they are never converted to zero. A manifest with missing or extra tasks,
the wrong corpus fingerprint, duplicate task IDs, or an invalid defect match fails closed.

## Comparison rules

- Freeze corpus before comparison.
- Use same task order, timeouts, budgets, and environment.
- Record model, provider, agent version, operating system, and commit SHA.
- Publish all attempts in the selected cohort.
- Do not remove failures.
- Do not compare costs when usage coverage differs materially.
- Report limitations beside results.

## Multi-agent gate

Specialist agents may ship only when they improve the agreed primary quality metric without
breaking cost and latency caps. Until then, the single-agent path remains default.


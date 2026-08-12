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


# Production readiness

Status date: 2026-09-03

The core is merged, but the service is not ready for production. Track the release gate in
[issue #46](https://github.com/Rajveerx11/pr-reliability-platform/issues/46).

## Product boundary

This is an approval-first AI pull request reviewer. It can run selected repository checks as
review evidence. It is not a general CI/CD platform or deployment system.

## Ready in the repository

- Signed GitHub webhook intake and durable Temporal runs.
- Context selection and provider-neutral review contracts.
- Disposable sandbox and Proof of Work evidence gate.
- Human approval and idempotent publishing boundaries.
- Private dashboard, telemetry, health checks, and VM deployment kit.
- Frozen evaluation corpus and deterministic harness replay.

## Required work

- Runtime: production provider and GitHub operations (#36), live publishing acceptance (#12),
  and Windows Temporal stability (#47).
- Repository experience: installation sync and policy (#37), repository and PR history (#38),
  Check Runs (#40), and GitHub login (#44).
- Evidence and operations: persisted analytics (#39), repository-defined checks (#41), bounded
  test evidence (#42), and runner operations (#43).
- Release proof: signed images (#45), real-provider evaluation (#14), and private Linux VM
  acceptance (#15).

## Recommended order

1. Provider and GitHub operations.
2. Installation sync, repository policy, and GitHub login.
3. Check Runs and repository-defined sandbox checks.
4. Evidence, metrics, runner operations, and dashboard history.
5. Signed images and release manifest.
6. Real-provider evaluation and private VM acceptance.

## Exit checklist

- Required Linux checks pass.
- A test repository completes webhook, review, verification, approval, and exactly one publish.
- Forked pull requests receive no secrets.
- Dashboard access uses GitHub identity and owner-scoped authorization.
- Repository configuration cannot expand network, secrets, image, or resource limits.
- Usage, cost, retry, queue, and failure facts are stored with explicit unknown values.
- Images are immutable, signed, scanned, and recorded in a release manifest.
- Backup, restore, rollback, monitoring, and credential rotation are exercised on Linux.
- A real-provider evaluation report is published with limitations.

## Rollout

Start with one private test repository in observation mode. Keep the GitHub check non-blocking.
Make it required only after quality, latency, cost, capacity, and recovery are acceptable. Add
repositories one at a time.

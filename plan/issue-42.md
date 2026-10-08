# Issue #42 — Bounded verification evidence

Issue: <https://github.com/Rajveerx11/pr-reliability-platform/issues/42>
Decision: [DEC-014](v1.md#dec-014--limit-ci-style-work-to-review-evidence)

## Scope

Retain encrypted, redacted and bounded check logs and JUnit summaries. Product receipts contain
only opaque artifact references and safe check facts. Dashboard findings link to their run's
verification checks; this does not claim a particular test proves an individual finding.
No runner operations, alerting, release flow or history redesign is included.
Migration 0009 is reserved for this issue.

## Acceptance checks

- Capture existing combined stdout/stderr limits and mark every truncated stream.
- Count JUnit testcase pass, failure/error, skip and finite durations. Reject DTDs, entities,
  excessive nesting, element counts, oversized files and malformed reports.
- Only the operator allowlist can select report XML basenames directly in `/workspace`.
  A live disposable container exports bounded regular files before cleanup. Read-only descriptor
  checks reject symlinks, hardlinks, directories and files modified during extraction.
- Store logs and report summaries only as authenticated ciphertext in `verification_artifacts`.
  Keep references in the verification receipt. Bound plaintext and retention by operator settings.
- Authenticate display/download, scope by owner and live repository permissions, refuse expired
  or corrupt evidence, and return no-store JSON with nosniff.
- Clear expired ciphertext and append one auditable `evidence.expired` event in the same transaction.
- Show each finding's run-level verification summary/log links and explicit expiry/unavailability.

## Validation boundaries

Unit tests run on Windows. PostgreSQL tests use a disposable task-owned native PostgreSQL 18
instance when available. Real Linux Docker tests remain required in the sandbox CI job: a local
Docker daemon is not available. Browser checks use local fixture responses, not staging evidence.
Independent review, CodeRabbit and remote CI belong to the parent handoff.
See [operator notes](../docs/verification-evidence.md) for the required deployment seams.

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

Validated on Windows with Python 3.12 and a disposable task-owned native PostgreSQL 18 instance:

- Focused suite: 149 passed, 21 skipped, one upstream Starlette deprecation warning.
- Full `pytest -q -ra`, with no test exclusions: 572 passed, 28 skipped, the same warning.
- `ruff check .`, `ruff format --check .` (202 files), Python compileall, Node syntax check,
  lock check, wheel/sdist build and wheel contents check passed.
- The expiry CLI ran successfully against the task-owned database after migration 0009.
- Playwright fixture checks passed at 1440px and 390px: keyboard summary/log display, literal
  hostile text, bounded log layout, expired evidence, attachment download, and a 401 during
  evidence listing without redisplaying already-fetched findings. No page errors.

Earlier runs failed honestly: large parametrized test IDs exceeded Windows environment limits;
shared test resource dictionaries leaked mutation; Windows Git marked fixture pack files
read-only; and a new eager package import broke stdlib-only release preflight. These were fixed
in test fixtures and the artifact import boundary, without changing release preflight or
production cleanup. Final focused and broad runs passed with the stated skips.

The full-suite skips are 13 real Docker checks (including five new report-export/attack cases),
six Linux-only file descriptor checks, seven Linux descendant-supervision checks and two POSIX
socket-ownership checks. A local Docker daemon is not available; Linux report extraction remains
an acceptance gap until sandbox CI runs. LSP tools are not available in this worker.
Browser checks are fixture evidence, not staging or real GitHub evidence. Independent review,
CodeRabbit and remote CI belong to the parent handoff.

Local artifacts: `C:/Users/rajve/AppData/Local/Temp/issue42-focused-final.log`,
`issue42-broad-final.log`, `issue42-browser-evidence.js`, and
`issue42-browser-evidence-output/report.json` in that same temp directory. Browser screenshots
and downloaded fixture JSON are beside the report. Checkpoint commit: `9cc86d4`.
See [operator notes](../docs/verification-evidence.md) for the required deployment seams.

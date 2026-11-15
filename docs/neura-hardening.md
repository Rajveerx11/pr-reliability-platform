# Neura integration hardening

Source baseline: `d1662e270e3d49087ba43025394d7ecf1a779200`, checked 2026-11-14.
Tracked by [#65](https://github.com/Rajveerx11/pr-reliability-platform/issues/65).

**No licence file exists. GitHub reports `license: null`. Reuse is blocked pending the owner's
licence decision. No licence was added.**

## Findings and changes

1. Confirmed fabricated verification: inbox and dashboard derived pass/fail from model evidence
   exit codes. All finding evidence now has `verification: "claimed"`. Historical JSON rows load
   with that default. The model cannot supply a passed/failed evidence status. Private views and
   GitHub finding text label claims. Platform sandbox artifacts keep their separate execution facts.
2. Confirmed missing prompt boundary. PR content is JSON-encoded inside explicit delimiters.
   Angle brackets inside content are escaped. Instructions forbid treating PR text as authority
   or changing the output contract. No live-model resistance claim is made.
3. Confirmed detector-only Proof of Work v0.2.0, with tests off. Published text and documentation
   state that limit. Stored detector metadata says `mode: detector_only`, `tests_executed: false`.
   Dependency and execution flags are unchanged. v0.3.0 with tests on remains pending.
4. Added `/api/findings/approved.jsonl` using existing session authorization and live repository
   access. It excludes pending/rejected findings and model evidence. PR outcome is `null` because
   merged versus closed is not persisted. No migration or runtime dependency was added.
5. Updated status and readiness against the source baseline. API documentation now lists evidence
   and operations routes. Implementation does not establish production acceptance.

## Local checks

Windows, Python 3.12.13, PostgreSQL 18.1. Tests used a disposable loopback database:

```powershell
uv sync --frozen --extra dev --python 3.12
$env:TEST_DATABASE_URL = 'postgresql://postgres@127.0.0.1:55465/pr_reliability_test'
uv run pytest --deselect=workers/tests/ -q --tb=short
uv run pytest workers/tests -q --tb=short
uv run pytest workers/tests/test_production_operations.py packages/proof_adapter/tests/test_detector_only.py -q --tb=short
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q apps workers packages evals
uv build
```

- Non-worker suite: 587 passed, 9 skipped, 368 deselected. Existing FastAPI/httpx deprecation warning.
- Worker suite: 328 passed, 40 skipped. POSIX-only and opt-in real Docker tests remain unexecuted locally.
- Final persistence/detector checks: 7 passed. Lint and formatting passed. Syntax checks and the distribution build passed.
- Focused API/contracts/publishing suite: 133 passed before the last two annotation exclusion tests.
- Before fixes, `uv run pytest apps/api/tests/test_finding_claims.py workers/tests/agents/test_review_agent.py -q`
  produced 13 failures and 8 passes. Fabricated zero exit status and injected closing delimiters were covered.
- Before the export existed, `uv run pytest apps/api/tests/auth/test_findings_export.py -q --tb=short`
  produced 9 failures. The completed suite covers missing/expired/revoked sessions, owner scope,
  inaccessible repositories, stale synchronization, pending/rejected decisions and historical heads.

Browser regression command:

```powershell
$env:DASHBOARD_TEST_ROOT = (Get-Location).Path
$browserTest = Join-Path $env:TEMP 'pr65-dashboard.browser.js'
Copy-Item -LiteralPath apps/web/tests/dashboard.browser.js -Destination $browserTest
node C:/Users/rajve/.agents/skills/playwright-skill/run.js $browserTest
```

Result: 66 assertions passed at desktop and mobile widths. Claim labels and claimed command exits
render as text. A historical `passed` label does not appear as a verification badge. Injected HTML
does not create image elements. Existing evidence controls and keyboard navigation passed.

`docker compose --env-file infra/deployment/deployment.env.example --file infra/deployment/compose.vm.yaml config --quiet`
passed. `docker compose -f infra/compose/compose.yaml config --quiet` passed with the fixture
environment from the `repository-shape` Quality job. Required repository files exist.
`docker version --format '{{.Server.Os}}'` failed because `dockerDesktopLinuxEngine` was absent.
No engine or VM was started. Image builds and real sandbox isolation require Linux CI.

## Review and remaining limits

OCR delegation supplied selection and rules. This agent reviewed source and diffs without an OCR
model endpoint. All 36 changed files were reviewed: 14 selected by OCR and 22 tests/docs
reviewed manually despite selector exclusions. Reviewed 36, skipped 0. This is self-review,
not independent review. CodeRabbit 0.8.1 is installed, but its hosted review was not invoked under the
task's no-real-provider constraint. No subagents or independent model review ran. Required
independent review remains open. SlopMonster lint ran; existing prose triggers style findings.
Edited prose was checked manually. Its different-model cleanse was not run under the same constraint.

Publication queries require matching approved decisions. Regression tests keep pending/rejected
claims out of reviews and annotations. Check Run lifecycle metadata is published before approval,
as already implemented. It contains fixed platform text. Approved model claim text still reaches
GitHub, now labelled unverified. No additional unapproved finding-content path was found in the
reviewed publication code.

An existing annotation coverage defect remains: `Evidence.end_line` defaults to `None`, while
`activities/checks.py` uses `evidence.get("end_line", start)` and rejects `None`. Thus an otherwise
valid approved source location can produce no annotation. A deterministic local reproduction
confirmed this. It is reported without a fix. Neura cannot assume every approved finding has an annotation.

Previously published GitHub text is not rewritten. In-flight receipts bind a body fingerprint.
Older prepared bodies can fail closed after the wording change. Export buffers one owner's history
in memory. Stream or paginate it before large histories. Journal ingestion is not built.

Issue #46 still leaves #12 unchecked. #12 closed on 2026-09-03 after PR49 merged. The tracker also
lags other merged work. No existing issues were edited, closed or reopened. No deployment, release,
secret change, real model call, or subscription-route activation occurred.

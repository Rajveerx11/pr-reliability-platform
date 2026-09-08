# Current status

Verified: 2026-09-08.

The review core, production OpenAI/GitHub operations, and repository installation sync and
policy are merged, and commit-bound Check Runs are implemented. Private production rollout is
still unaccepted. Repository history UI, complete analytics, bounded check artifacts, signed
releases, real-provider evaluation, and Linux VM recovery evidence remain open.

## Implemented

- Strict versioned contracts and seven checksummed PostgreSQL migrations.
- Signed, installation-bound PR and installation lifecycle webhooks, with delivery deduplication.
- Initial and periodic installation inventory, owner-scoped policy API, and append-only audit.
- Admission and queued-dispatch checks for access, pause state, base branch, budgets, and sync age.
- Durable Temporal execution, ordered run generations, retries, cancellation, and supersession.
- Bounded context selection, OpenAI structured findings, and provider-reported usage facts.
- Exact-head GitHub checkout, disposable Linux sandbox, and Proof of Work verification.
- Human approval and idempotent, commit-bound GitHub review publication.
- Idempotent Check Runs with safe annotations, exact-run dashboard links, and authenticated reruns.
- Private run dashboard and approval inbox using individual revocable GitHub sessions.
- Telemetry and readiness for PostgreSQL, Temporal, and installation sync freshness.
- Frozen ten-task corpus, deterministic evaluation replay, and private VM deployment tools.

## Recently completed issues

| Issue | Delivered | Merged PR |
|---|---|---|
| [#12](https://github.com/Rajveerx11/pr-reliability-platform/issues/12) | Approval-bound GitHub publishing | [#49](https://github.com/Rajveerx11/pr-reliability-platform/pull/49) |
| [#36](https://github.com/Rajveerx11/pr-reliability-platform/issues/36) | Production provider operations | [#50](https://github.com/Rajveerx11/pr-reliability-platform/pull/50) |
| [#37](https://github.com/Rajveerx11/pr-reliability-platform/issues/37) | Installation sync and repository policy | [#51](https://github.com/Rajveerx11/pr-reliability-platform/pull/51) |
| [#47](https://github.com/Rajveerx11/pr-reliability-platform/issues/47) | Windows Temporal regression stability | [#48](https://github.com/Rajveerx11/pr-reliability-platform/pull/48) |

## Verification evidence

All five Quality jobs passed on the merged baseline in
[main CI run 34090550220](https://github.com/Rajveerx11/pr-reliability-platform/actions/runs/34090550220).

| Job | Result |
|---|---|
| repository-shape | Required files, syntax, both Compose manifests, and image build passed |
| python-quality | Ruff lint/format passed; 411 tests passed, 8 skipped |
| temporal-workflow | 183 passed, 8 skipped on Linux |
| temporal-workflow-windows | 16 workflow regression tests passed |
| sandbox-integration | 35 real Docker sandbox tests passed |

Jobs overlap; do not add their counts into one unique test total. Skipped tests are not evidence
for the skipped boundary. The dedicated sandbox job exercises the real container boundary.
PR #51 also completed independent review and Greptile review at 5/5, with its finding resolved.

A full local Windows run encountered an existing Git-pack cleanup `PermissionError` in the
production-operations fixture. It was reproduced on untouched base `09da81a`; Linux CI passes
that test. The focused Windows workflow job does not prove the entire application suite works
on Windows. See [development](development.md).

## Remaining work

Open feature issues are #38 through #45. [#14](https://github.com/Rajveerx11/pr-reliability-platform/issues/14)
requires real model evaluation; [#15](https://github.com/Rajveerx11/pr-reliability-platform/issues/15)
requires private Linux VM acceptance. [#46](https://github.com/Rajveerx11/pr-reliability-platform/issues/46)
tracks the production release gate. Its original checklist may lag closed child issues; this
snapshot uses live issue states and merged code.

Repository inventory and policy are APIs today; the repository/history UI is still #38.
Only the operator-configured `default` verification profile is supported. Model quality and
exact billed cost remain unmeasured. No real deployment, backup restore, or live-provider
acceptance is claimed by repository CI. See [production readiness](production-readiness.md).

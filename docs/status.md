# Current status

Source baseline: merged `main` at `d1662e2`, checked 2026-11-14.
PR62/#45 is merged release tooling, not live release acceptance. The #65 hardening changes
label model evidence as claims, delimit untrusted context, and add an approved-findings export.
The earlier CI evidence below applies only to its recorded commit, not this integration.

The review core, OpenAI API/GitHub operations, installation policy, repository checks, Check Runs,
review metrics, bounded encrypted artifacts and runner operations are merged. Private production
rollout is not accepted. Repository history UI, real Linux signed release artifacts, staging,
backup restore, rollback, protected environments, real-provider evaluation and VM acceptance
remain open. Release publication requires separate approval. ChatGPT-subscription Codex reviews
remain disabled until isolated execution and repository/fork validation gates are implemented
and approved.

## Implemented

- Strict versioned contracts and ten checksummed PostgreSQL migrations.
- Bounded encrypted/redacted check artifacts, private display/download and expiry maintenance.
- Private runner capacity, pending queue observations, alerts, probes and backup receipt wiring.
- Signed, installation-bound PR and installation lifecycle webhooks, with delivery deduplication.
- Initial and periodic installation inventory, owner-scoped policy API, and append-only audit.
- Admission and queued-dispatch checks for access, pause state, base branch, budgets, and sync age.
- Durable Temporal execution, ordered run generations, retries, cancellation, and supersession.
- Bounded context selection, OpenAI structured findings, and provider-reported usage facts.
- Exact-head GitHub checkout, disposable Linux sandbox, and Proof of Work v0.2.0 detector-only checks (no package tests).
- Human approval and idempotent, commit-bound GitHub review publication.
- Authenticated JSON Lines export of approved structured findings; PR outcome stays unknown.
- Model evidence labelled `claimed`, including historical rows. Approval does not verify it.
- Idempotent Check Runs with safe annotations, exact-run dashboard links, and authenticated reruns.
- Private run dashboard and approval inbox using individual revocable GitHub sessions.
- Nullable review durations, activity attempts and retries, usage coverage, and known cost on the
  dashboard; facts unavailable from a provider or Temporal history remain unknown.
- Telemetry and readiness for PostgreSQL, Temporal, and installation sync freshness.
- Frozen ten-task corpus, deterministic evaluation replay, and private VM deployment tools.

## Recently completed issues

| Issue | Delivered | Merged PR |
|---|---|---|
| [#12](https://github.com/Rajveerx11/pr-reliability-platform/issues/12) | Approval-bound GitHub publishing | [#49](https://github.com/Rajveerx11/pr-reliability-platform/pull/49) |
| [#36](https://github.com/Rajveerx11/pr-reliability-platform/issues/36) | Production provider operations | [#50](https://github.com/Rajveerx11/pr-reliability-platform/pull/50) |
| [#37](https://github.com/Rajveerx11/pr-reliability-platform/issues/37) | Installation sync and repository policy | [#51](https://github.com/Rajveerx11/pr-reliability-platform/pull/51) |
| [#47](https://github.com/Rajveerx11/pr-reliability-platform/issues/47) | Windows Temporal regression stability | [#48](https://github.com/Rajveerx11/pr-reliability-platform/pull/48) |
| [#39](https://github.com/Rajveerx11/pr-reliability-platform/issues/39) | Review metrics and analytics persistence | [#57](https://github.com/Rajveerx11/pr-reliability-platform/pull/57) |
| [#42](https://github.com/Rajveerx11/pr-reliability-platform/issues/42) | Bounded encrypted verification artifacts | [#61](https://github.com/Rajveerx11/pr-reliability-platform/pull/61) |
| [#43](https://github.com/Rajveerx11/pr-reliability-platform/issues/43) | Runner visibility and private alerts | [#63](https://github.com/Rajveerx11/pr-reliability-platform/pull/63) |

## Verification evidence

All five Quality jobs passed on merged commit `c9f67e3` in
[main CI run 36257135195](https://github.com/Rajveerx11/pr-reliability-platform/actions/runs/36257135195):
`repository-shape`, `python-quality`, `temporal-workflow`, `temporal-workflow-windows`, and
`sandbox-integration`. A passing repository CI run is not a live-provider or VM acceptance test.
Skipped tests do not establish coverage for their skipped boundary.

## Remaining work

No licence file is present. Reuse is blocked pending the owner's licence choice; this task adds
none. Tracker #46 still leaves #12 unchecked although #12 is closed and PR49 merged. Its checklist
also lags merged #36/#37/#39–#44/#47 work. No existing issues were edited.

Prompt delimiting and deterministic tests establish the input boundary, not live-model injection
resistance. Model evidence remains unverified after approval. Proof of Work v0.2.0 is detector-only;
moving to v0.3.0 with tests on is pending a release and sandbox integration. PR merge outcomes,
Neura consumption and automatic learning-journal ingestion are not built or validated here.

Repository and PR history UI [#38](https://github.com/Rajveerx11/pr-reliability-platform/issues/38)
and live signed release acceptance [#45](https://github.com/Rajveerx11/pr-reliability-platform/issues/45)
remain open. Release tooling in PR62 is not proof of genuine signed artifacts or Linux staging.
Merged #42/#43 implementation also does not establish live operator acceptance.
Real model evaluation [#14](https://github.com/Rajveerx11/pr-reliability-platform/issues/14)
and Linux VM acceptance [#15](https://github.com/Rajveerx11/pr-reliability-platform/issues/15)
are not complete; [#46](https://github.com/Rajveerx11/pr-reliability-platform/issues/46)
tracks the production gate.

The merged OpenAI provider uses an API key, not a ChatGPT subscription. The
[Codex private pilot (#55)](https://github.com/Rajveerx11/pr-reliability-platform/issues/55)
in [PR #56](https://github.com/Rajveerx11/pr-reliability-platform/pull/56) is still disabled pending
[subscription runner (#58)](https://github.com/Rajveerx11/pr-reliability-platform/issues/58)
and [repository admission (#59)](https://github.com/Rajveerx11/pr-reliability-platform/issues/59).
[Public repository opt-in (#60)](https://github.com/Rajveerx11/pr-reliability-platform/issues/60)
requires its own security gate. Only the operator-configured `default` verification profile is
supported. Model quality and exact billed cost remain unmeasured. Repository CI does not prove a
live deployment, backup restore, or provider acceptance. See [production readiness](production-readiness.md).

# Production readiness

Integration baseline: merged `main` at `ee8b6d6`; PR62 release tooling is in this candidate.
Review and CI for the candidate are still required. Release tracker:
[#46](https://github.com/Rajveerx11/pr-reliability-platform/issues/46).

The review core, OpenAI API/GitHub operations (#36), approved publishing (#12), installation
policy (#37), repository checks (#41), Check Runs (#40), GitHub sessions (#44), and review
metrics (#39), bounded evidence (#42/PR61), and runner operations (#43/PR63) are merged.
Release tooling (#45/PR62) is implemented in this candidate. These are prerequisites for
production, not proof that a live service
or measured model baseline exists. [Current status](status.md) records CI evidence.

## Remaining delivery

| Issue | Remaining outcome | Prerequisite status |
|---|---|---|
| #38 | Repository and PR history dashboard | #37 merged |
| #42 | Live evidence settings and retention acceptance as part of #15 | Implementation merged in PR61 |
| #43 | Live receiver, queue monitoring and recovery acceptance as part of #15 | Implementation merged in PR63 |
| #45 | Genuine Linux signed artifacts, staging, backup restore, rollback and protected environments | Release tooling implemented in candidate PR62; live acceptance open |
| #14 | Frozen real-provider evaluation report | Provider code ready; real runs and adjudication needed |
| #15 | Private Linux VM end-to-end and recovery acceptance | Remaining release gates must pass |

The full dependency map is in [plan/v1.md](../plan/v1.md#github-issue-map). Work on unblocked
issues; do not bypass dependencies. Complete history and live evidence, runner operations,
release and GitHub login acceptance. Release workflows are manual-only; publication requires
separate approval. Finish real evaluation and VM acceptance before any production claim.
ChatGPT-subscription Codex remains disabled in open PR #56; its private pilot depends on #58 and
#59, and public repository opt-in requires #60.

## Production exit checklist

- Approved release commit passes the configured Linux and focused Windows CI jobs.
- A dedicated test repository completes sync, signed webhook, review, verification, human approval,
  Check Run reporting, and exactly-once publication on the reviewed commit.
- Removed, paused, suspended, and stale installations cannot admit or dispatch new reviews.
- Fork PR checks receive no provider, GitHub, database, or runner credentials.
- GitHub login and owner-scoped authorization protect dashboard and approval operations.
- Repository check configuration cannot exceed operator-approved images, commands, or limits.
- Usage, retry, queue, and failure facts are stored; unavailable facts remain unknown.
- A frozen real-provider report records quality, latency, usage coverage, cost, and limitations.
- Signed, scanned immutable images match an approved release manifest.
- Private TLS, monitoring, alerts, secret rotation, backup, restore, and rollback are exercised on Linux.
- No unresolved critical or high-severity security finding remains.
- Issues #14 and #15 close with evidence; #46 records accepted release evidence. #12 is already closed.

## Current rollout constraints

Start an acceptance deployment with one private test repository. Migration 0005 starts existing
repository access as pending; complete the first sync before delivering test PRs. Sync runs at
startup and every 60 seconds, with a 15-minute freshness limit. Readiness returns 503 when sync
is missing or expired. See [repository policy](repository-policy.md) and [deployment](deployment.md).

Policy changes govern new admissions and queued dispatch. They do not cancel running reviews.
Blocked webhook deliveries are recorded without deferred execution; a new PR event is needed
after recovery. Individual GitHub sessions replace the shared reviewer token; live login acceptance
remains part of #15. Start GitHub Check Runs in informational mode before considering required-check
enforcement.
This product does not replace general CI/CD or deploy customer applications.

# Production readiness

Status date: 2026-09-07. Baseline: `675257a`. Release tracker:
[#46](https://github.com/Rajveerx11/pr-reliability-platform/issues/46).

The AI pull request review core, provider operations (#36), approved publishing (#12), and
installation sync/policy (#37) are merged. They are prerequisites for production, not proof
that a live service or measured model baseline exists. [Current status](status.md) records CI evidence.

## Remaining delivery

| Issue | Remaining outcome | Prerequisite status |
|---|---|---|
| #38 | Repository and PR history dashboard | #37 merged |
| #39 | Durable analytics, usage, retry, and cost facts | #36 merged |
| #40 | Commit-bound GitHub Check Runs | #12 and #36 merged |
| #41 | Repository-defined sandbox verification checks | #37 merged |
| #42 | Retained bounded logs and test summaries | Requires #41 |
| #43 | Runner capacity, queue visibility, and alert delivery | Requires #39 and #41 |
| #44 | Individual GitHub identity and sessions | #37 merged |
| #45 | Signed immutable release images and manifest | #36 merged |
| #14 | Frozen real-provider evaluation report | Provider code ready; real runs and adjudication needed |
| #15 | Private Linux VM end-to-end and recovery acceptance | Remaining release gates must pass |

The full dependency map is in [plan/v1.md](../plan/v1.md#github-issue-map). Work on unblocked
issues; do not bypass dependencies. Prioritize individual access (#44), Check Runs (#40), and
repository checks (#41), then complete evidence, metrics, history, runner operations, and releases.
Finish real evaluation and VM acceptance before any production claim.

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
after recovery. The shared reviewer token is temporary. GitHub Check Runs are not yet implemented;
after #40 ships, start them in informational mode before considering required-check enforcement.
This product does not replace general CI/CD or deploy customer applications.

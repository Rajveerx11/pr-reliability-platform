# Documentation

Verified against merged `main` at `675257a` on 2026-09-07. Start with [current status](status.md).

## Build and operate

- [Development](development.md): install, configure, migrate, start processes, and run tests.
- [Configuration](configuration.md): environment variables and process-specific requirements.
- [API](api.md): routes, authorization, webhook behavior, and contract references.
- [Repository policy](repository-policy.md): installation sync, admission, policy, audit, and recovery.
- [Dashboard](dashboard.md): implemented views and remaining UI work.
- [Observability](observability.md): tracing, metrics, readiness, and sync diagnostics.
- [Deployment](deployment.md): private Linux VM, preflight, backup, restore, and rollback.

## Design and acceptance

- [Architecture](architecture.md): components, ownership, persistence, and execution boundaries.
- [Security](security.md): credentials, sandboxing, approval, and operational trust.
- [Review checks](review-checks.md): current sandbox and planned CI-style evidence scope.
- [Evaluation methodology](evaluation.md): frozen tasks, scoring, and comparison rules.
- [Historical evaluation report](evaluation-report.md): deterministic replay, not model-quality proof.
- [Production readiness](production-readiness.md): open work and evidence required for release.
- [Version-one plan](../plan/v1.md) and [interactive plan](../plan/interactive.html).
- [Change history](../Changes.md).

Plans live in `plan/`; operational documentation lives here. Planned behavior is labelled and
linked to its issue. GitHub issue #46 tracks the remaining production gate.

# Changes

This file records repository changes. Each entry links changes to the decision that caused
them and names the affected files.

## 2026-08-12 — Repository foundation

### Added private project repository

- Decision: [DEC-010 — Repository structure](plan/v1.md#dec-010--use-clear-code-plan-and-documentation-boundaries)
- Reason: Keep code, plans, documentation, evaluations, and infrastructure easy to find.
- Changed files:
  - `README.md`
  - `AGENTS.md`
  - `.gitignore`
  - `.editorconfig`
  - `.env.example`
  - `pyproject.toml`
  - Code boundary folders under `apps/`, `workers/`, and `packages/`
  - Supporting folders under `evals/`, `infra/`, `migrations/`, and `scripts/`

### Added version-one plan and decision log

- Decisions: [DEC-001 through DEC-013](plan/v1.md#decisions)
- Reason: Freeze important product, workflow, safety, data, and delivery choices before code.
- Changed files:
  - `plan/README.md`
  - `plan/v1.md`
  - `plan/interactive.html`

### Added project documentation

- Decisions: [DEC-005](plan/v1.md#dec-005--use-postgresql-with-stable-ownership-fields), [DEC-008](plan/v1.md#dec-008--run-untrusted-commands-inside-a-real-sandbox), [DEC-009](plan/v1.md#dec-009--require-human-approval-before-every-external-write), [DEC-010](plan/v1.md#dec-010--use-clear-code-plan-and-documentation-boundaries)
- Reason: Record architecture, setup, security, and evaluation rules outside source code.
- Changed files:
  - `docs/README.md`
  - `docs/architecture.md`
  - `docs/development.md`
  - `docs/security.md`
  - `docs/evaluation.md`

### Added GitHub issue workflow

- Decision: [DEC-011 — GitHub issues](plan/v1.md#dec-011--track-every-feature-with-a-github-issue)
- Reason: Give each feature a clear scope, acceptance criteria, dependencies, and history.
- Changed files:
  - `.github/ISSUE_TEMPLATE/feature.yml`
  - `.github/ISSUE_TEMPLATE/bug.yml`
  - `.github/ISSUE_TEMPLATE/config.yml`
  - `.github/workflows/quality.yml`


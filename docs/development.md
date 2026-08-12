# Development Guide

## Requirements

- Python 3.12 or newer
- Git
- Docker with Compose
- GitHub CLI for repository administration
- Temporal development server or local container
- PostgreSQL local container

## Folder rules

- `apps/api/`: webhook and product API.
- `apps/web/`: approval and run interface.
- `workers/`: Temporal workflows and activities.
- `packages/contracts/`: shared versioned data shapes.
- `packages/proof_adapter/`: Proof of Work integration.
- `evals/golden_prs/`: frozen evaluation fixtures.
- `infra/`: local and deployment infrastructure.
- `migrations/`: PostgreSQL migrations.
- `plan/`: product plans and decisions.
- `docs/`: technical documentation.

Keep each file focused on one responsibility. Prefer small modules, but do not split one simple
idea across many tiny files. A file should be easy to understand in one sitting.

## Feature workflow

1. Open or choose one GitHub issue.
2. Confirm acceptance criteria and linked decision.
3. Create a focused branch.
4. Add tests with implementation.
5. Update relevant documentation.
6. Add one entry to `Changes.md` with issue, decision, and changed files.
7. Open a pull request linked to the issue.
8. Merge only after required checks pass and a human approves.

## Local configuration

Copy `.env.example` to `.env`. Never commit `.env`, API keys, GitHub private keys, or tokens.
Local containers must use development-only credentials.

## Quality commands

Commands will be finalized with the first implementation issue. Expected checks are:

```text
ruff check .
ruff format --check .
pytest
```

Integration tests must use isolated databases and queues. End-to-end tests must use a test
GitHub App or recorded fixture, never a production repository.


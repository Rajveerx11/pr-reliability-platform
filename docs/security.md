# Security

## Trust model

Pull request code is untrusted. Agent output is untrusted. Webhook bodies are untrusted until
their GitHub signature is verified.

## Required controls

- Verify webhook signature before parsing event details.
- Resolve every repository through its GitHub installation and owner.
- Use short-lived, repository-scoped GitHub tokens.
- Keep provider and GitHub secrets outside prompts and logs.
- Run pull request commands in disposable containers.
- Block network access by default inside the sandbox.
- Apply CPU, memory, process, output, filesystem, and time limits.
- Bind all findings and actions to one head SHA.
- Require human approval before every external write.
- Make publish actions idempotent.
- Fail closed when validation, sandboxing, tests, or Proof of Work fails.

## Stored data

Allowed:

- Repository and pull request identifiers
- Commit SHAs
- Structured findings and safe evidence references
- Run status, timings, token counts, and exact reported cost
- Approval and publish audit events

Not allowed:

- GitHub App private keys
- Provider API keys
- Raw repository contents
- Full prompts or agent transcripts
- Sandbox filesystem snapshots
- Secrets found in source or output

## Incident rule

If a write happens without approval, disable publishing immediately, preserve safe audit facts,
rotate affected credentials, and document the event before restoring writes.


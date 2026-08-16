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
- Authenticate approval API calls with a dedicated reviewer token and server-bound actor identity.
- Require human approval before every external write.
- Make publish actions idempotent.
- Fail closed when validation, sandboxing, tests, or Proof of Work fails.

## Sandbox operations

- Use a dedicated Linux Docker engine for sandbox workloads. Access to a Docker daemon is a
  privileged control-plane capability; never expose its socket, API, credentials, or host mounts
  inside a pull request container.
- Configure only reviewed images addressed by a full `sha256` image ID or repository digest.
  Mutable tags such as `latest` are rejected.
- Preinstall required test runtimes and dependencies in the reviewed image. The sandbox has no
  network and must not receive provider, GitHub, database, or Docker credentials.
- The worker accepts command arguments, not a host shell command. A requested shell may run only
  inside the sandbox container.
- Source is copied without `.git` into a temporary read-only mount under byte, entry-count, and
  staging-time limits. The command runs from a second, size-limited tmpfs copy that disappears with
  the container.
- Verification activity providers must use `SandboxVerificationOperation`. Its prepare and record
  callbacks may resolve inputs and persist bounded results, but must never run pull request code.
- Treat timeout, output overflow, non-zero exit, runtime failure, and cleanup failure as distinct
  failed-verification evidence. After recording bounded command evidence, raise a non-retryable
  verification failure. Never fall back to host execution.
- Production startup accepts only the default Docker CLI runner. It requires Linux plus memory,
  swap, CPU-quota, and PID-limit support before any pull request source is staged or executed.

## Stored data

Allowed:

- Repository and pull request identifiers
- Commit SHAs
- Structured findings and safe evidence references
- Run status, timings, token counts, and exact reported cost
- Approval and publish audit events

The approval page never stores its reviewer token in browser storage. API queries scope every row
to the configured owner. A decision transaction locks the pull request row, so a concurrent head
update cannot race a stale approval into storage. The approval endpoint records audit and durable
workflow-signal events but performs no external write.

## GitHub comment publishing

- The activity accepts only the stable `{run_id}:{head_sha}:publish` idempotency key.
- Every selected finding needs one matching `approved` decision for the same owner, run, and head.
- Both the stored pull request head and GitHub's current head must match before comment creation.
- The comment contains only reviewer-approved finding claims plus a hashed retry marker.
- A retry searches only the authenticated GitHub App identity's comments for that marker before
  creating a comment and reuses the existing remote ID. The marker is not an authorization secret.
- Audit events store bounded IDs, commit SHA, and result codes. They never store the comment body,
  GitHub token, response body, or exception text.

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


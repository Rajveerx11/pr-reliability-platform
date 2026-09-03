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

## GitHub review publishing

- The activity accepts only the stable `{run_id}:{head_sha}:publish` idempotency key.
- A publish request must contain a non-empty, one-to-one finding and approval set; malformed
  run-bound requests fail before GitHub access and record only a bounded reason code.
- Every selected finding needs one matching `approved` decision for the same owner, run, and head.
- The dispatcher waits until every finding has an immutable decision. It sends one ordered
  approval set: mixed decisions publish only approved findings, while an all-rejected set
  terminates without a GitHub write.
- The REST client sends its bearer token only to `https://api.github.com`. Custom API origins are
  rejected before any request.
- Both the stored pull request head and GitHub's current head must match before review staging.
- The client stages an unsubmitted `PENDING` review with the approved SHA as `commit_id`, rechecks
  the head, and deletes the draft and fails closed on drift. Only a match allows event `COMMENT`
  submission, so a change during the original precheck-to-create window exposes no public review.
- GitHub offers no conditional review mutation, so the final head read and submit call cannot be
  atomic. If the head changes in that provider window, immutable `commit_id` still prevents the
  review from attaching to the newer commit.
- The review summary contains only reviewer-approved finding claims plus a hashed retry marker.
- A retry searches only the authenticated GitHub App identity's reviews and reuses the existing
  remote ID only when the commit SHA, terminal marker, and complete body match the approved
  rendering. Marker substrings, edited bodies, and reviews on other commits are rejected. The
  marker is not an authorization secret. Exact pending reviews are rechecked and submitted or
  deleted, never treated as published receipts.
- One database-backed session claim serializes lookup, create, and receipt recording for the
  stable publish key. A crashed worker releases the claim so marker recovery can continue safely.
- The action stores a canonical payload fingerprint. Reusing its key with different findings,
  approvals, body reference, or rendered review body fails before any remote lookup or write.
- Audit events store bounded IDs, commit SHA, and result codes. They never store the review body,
  GitHub token, response body, or exception text. Provider exceptions are removed before Temporal
  records an activity failure.
- Authorization, wrong-state, and stale-head blocks append `github.review_publish_blocked` with
  one bounded reason code only. Provider failures append a fixed failure code; neither event stores
  body, token, response, or exception text.

Not allowed:

- GitHub App private keys
- Provider API keys
- Raw repository contents
- Full prompts or agent transcripts
- Sandbox filesystem snapshots
- Secrets found in source or output

## Production controls still required

- Replace the shared reviewer token with GitHub login and owner-scoped sessions (#44).
- Mint short-lived GitHub installation tokens only inside trusted provider operations (#36).
- Use only the GitHub permissions required for review comments and Check Runs (#40).
- Treat repository check configuration as untrusted. It cannot enable network, choose images,
  mount host paths, raise limits, or request secrets (#41).
- Forked pull request checks never receive provider, GitHub, database, or runner secrets.
- Redact, bound, and expire stored logs and test summaries (#42).
- Deploy only signed immutable images recorded in a release manifest (#45).

See [production readiness](production-readiness.md) for the full release gate.

## Incident rule

If a write happens without approval, disable publishing immediately, preserve safe audit facts,
rotate affected credentials, and document the event before restoring writes.

## Private VM operations

- Bind the only published application port, TLS 443, to one private VM address and restrict it to
  the approved VPN or private CIDR. Keep databases, Temporal, application ports, telemetry, and
  Docker sockets off public interfaces.
- Use certificate, key, provider, GitHub, and database secrets from root-owned files outside the
  checkout. Repository builds and backups must never include them.
- Address every runtime image by a reviewed SHA-256 digest. Run deployment preflight before pull,
  startup, rollback, or restore.
- Give the activity worker only a dedicated rootless sandbox Docker socket. The primary host Docker
  socket is a control-plane secret and must never enter an application container.
- Store backup bundles on encrypted storage outside the checkout, restrict them to operators, copy
  them off the VM, verify checksums before restore, and test recovery on a disposable VM.
- Treat VM root and Docker-administrator access as privileged production access. Preserve private
  firewall, certificate, backup, health, and rollback evidence without storing secret contents.

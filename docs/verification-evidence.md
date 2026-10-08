# Verification evidence

Issue [#42](https://github.com/Rajveerx11/pr-reliability-platform/issues/42),
[DEC-014](../plan/v1.md#dec-014--limit-ci-style-work-to-review-evidence).

## Deployment configuration

Set the same `EVIDENCE_ENCRYPTION_KEY` on the API and activity worker. Use a Fernet key generated
by `cryptography.fernet.Fernet.generate_key()`, stored in the operator's secret store, not Git.
Startup fails if the key is missing or invalid. Keep it available for the retention period;
replacing it without re-encryption makes older evidence unavailable. Both supported Compose
manifests forward the same evidence key, limits, retention and literal patterns to these services.
The VM preflight validates the key and settings before startup. The local activity service still
needs its existing reviewed provider/socket override; evidence wiring does not replace that.

Generate a separate key with `uv run python -c 'from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())'` in a private operator session. Save it as
`EVIDENCE_ENCRYPTION_KEY` only in the external mode-0600 environment file or secret store.
Never paste it into source, tickets, logs or CI examples. For local Compose, pass that external
file with `--env-file`; for the VM use the external deployment file and run preflight first.

- `EVIDENCE_MAX_BYTES`: maximum plaintext JSON bytes per check, default 262144, range
  4096–10485760. Log text gets a conservative part of this budget to allow worst-case JSON
  escaping. The existing sandbox's combined output bound applies first.
- `EVIDENCE_RETENTION_SECONDS`: default 604800 (7 days), range 60–7776000 (90 days).
- `EVIDENCE_SECRET_PATTERNS`: JSON list of literal secret strings, default `[]`. Patterns are
  literal, not regular expressions, to avoid hostile-output regex resource attacks. Redaction
  happens before evidence truncation/encryption and again using current settings at display/download.
  If runtime capture already cut a stream, any matching secret prefix at that boundary is also
  redacted. The combined stream limit can conservatively redact an intact stream's suffix too.

Schedule `pr-reliability-evidence-expire` at least every minute with `DATABASE_URL`. It clears
expired ciphertext in batches and records opaque-reference audit tombstones transactionally.
API reads also run bounded owner-scoped expiry sweeps. Expired content is never served, even if
maintenance is delayed. Database backups, replicas and WAL follow their own operator retention:
clearing live ciphertext does not erase historical backups. Compose and preflight now forward
and validate evidence settings. The operator must still schedule maintenance before rollout;
no release flow or scheduler was changed. There is no staging or deployment acceptance claim.

## Supported reports

The trusted `REVIEW_CHECK_ALLOWLIST_JSON` can add `"report_files": ["junit.xml"]` to a check.
The exact approved command must write those files directly inside `/workspace`, for example
`python -m pytest --junitxml=/workspace/junit.xml`. The repository config cannot override reports.
At most four XML files, each at most 1 MiB, are exported. Nested paths are not supported.

Approved report-enabled images must supply image-owned `python3` and `sleep`. The report-enabled
runner keeps the container's tmpfs alive, executes the exact approved command under the existing
isolation and output limits, reads reports with isolated Python and read-only no-follow file
handles, then destroys the container. Missing tools, files, malformed reports or extraction
errors block verification with a safe error code. No report is read from the host checkout.
Raw reports, testcase names, failure messages and source files are not persisted. JUnit
`testsuite`/`testsuites` roots and `testcase` children are supported; suite aggregate attributes
are not trusted. XML namespaces are unsupported anywhere, including attributes and unused
namespace declarations. Failure/error cases also block a successful command exit. DTDs/entities,
non-finite/negative/excessive durations, depth over 32 and more than 20000 XML elements fail safe.

## Reviewer access

The dashboard links every finding to its run's check summaries and bounded logs. These are
run-level checks, not a claim that a particular testcase proves that finding. JSON display and
attachment downloads use the existing reviewer session, owner scope and live repository scope.
Expired artifacts retain only metadata and an audit tombstone. Corrupt or wrong-key ciphertext
returns a safe unavailable response without exposing parser or runtime diagnostics.

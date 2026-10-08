# Signed releases (#45)

This is release machinery, not a record of a release or a deployment. Do not close
[#45](https://github.com/Rajveerx11/pr-reliability-platform/issues/45) from repository tests.
The decision is [DEC-015](../plan/v1.md#dec-015--require-production-evidence-before-rollout).
External writes still need [DEC-009](../plan/v1.md#dec-009--require-human-approval-before-every-external-write).

## Trust boundary

Only the reviewed `main` workflow identity, GitHub OIDC issuer, repository, workflow-dispatch
trigger, main ref and exact commit may sign a candidate. Cosign checks Fulcio and transparency
log evidence; there is no insecure verification switch or local "verified" boolean. A file
checksum or a successful workflow download is not a signature.

The signed `release.json` has a strict schema and records:

- Full commit SHA and all seven Compose images plus one sandbox image, by immutable digest.
- Every SQL migration's name and SHA-256, including already applied migrations.
- A configuration SHA-256 for the public Compose, proxy, entrypoints and telemetry configuration.
- The previous signed manifest's SHA-256 and commit, or `null` for bootstrap only.
- SHA-256 values for the three image vulnerability reports and three CycloneDX SBOMs.

Images are separate `ghcr.io/rajveerx11/pr-reliability-platform/platform`, `provider-activity`
and `sandbox` packages. The platform uses the existing Dockerfile. The provider image adds Git
from a fixed, signature-checked Debian snapshot and a digest-pinned official Docker CLI.
The sandbox contains only hash-locked Python dependencies (including pytest and Ruff), not
product source, Git, a Docker CLI or credentials. It still runs with the existing rootless,
network-denied sandbox policy. It is a baseline Python tool image, not a universal build image.

Every owned image must have a passing scan, signed image and signed CycloneDX attestation.
The authenticated manifest binds the local SBOM and scan bytes. Document verification uses
private snapshots of the exact parsed bytes, preventing source swaps during the verifier
subprocess; scan parsing and checksums also use the same byte snapshot. A registry attestation alone
is not a substitute for the attached reports. UNKNOWN, HIGH and CRITICAL vulnerabilities fail;
LOW and MEDIUM findings remain in the attached report, with no ignore-unfixed bypass.
Third-party service images are bound by the signed manifest; they are not re-signed as our code.

## Infrastructure needed before dispatch

A repository administrator must **separately approve and configure** these prerequisites.
This change does not change repository settings, grant permissions or deploy anything.

1. Protected environments `approved-release-build`, `disposable-linux-staging` and
   `approved-release-publication`, with required independent human reviewers, no self-approval,
   and only protected `main`. Environment names alone do not enforce approval.
2. Passing existing `Quality` CI for the exact main push. It is not replaced by release checks.
3. GHCR package creation/write permission for build; read permission for staging/publication.
   GitHub Actions OIDC permission for build/staging, plus network access to GitHub, GHCR,
   Sigstore/Fulcio/Rekor, Trivy databases, pinned image registries and Debian snapshots.
4. A dedicated **disposable Linux staging VM**, never production, with a trusted self-hosted
   runner labelled `pr-release-staging`, Docker/Compose, the separate rootless sandbox engine,
   TLS, PostgreSQL/Temporal, and external runtime secret files. Do not run PR code on this runner.
   Prevent concurrent manual operations on this VM during the staging run.
5. `/etc/pr-reliability/staging.env`, mode 0600, outside the checkout. It has the existing private
   deployment inputs and approved `DEPLOYMENT_CONFIG_VERSION`. Keep actual credentials private;
   do not pass them as workflow inputs or attach this file. Environment copies remain private
   temporary files outside the checkout and are removed when the stage command exits normally.
6. A root-managed executable `/opt/pr-reliability/staging-e2e` and public repository variable
   `STAGING_E2E_SHA256` with its independently reviewed checksum. This is a **real acceptance
   program**, not `true`, a readiness probe, a synthetic receipt or a mock. It accepts
   `--env-file PATH`, has bounded execution, returns nonzero on any missing proof and must test
   real GitHub/provider review, individual login/approval, durable data and Temporal state after
   restore, and the previous release after rollback. It must use only the authorized test
   repository and must not print secrets. This installation/real-provider proof is not supplied
   or claimed by these unit tests; coordinate the existing E2E/operations acceptance lanes.
7. An earlier signed, accepted compatible release. Bootstrap artifacts may be built without one,
   but **cannot pass staging/publication**. There is no fake initial rollback receipt.

Configuration hashes cover public checked-in deployment files, not secrets or every runtime
value. An operator must version and approve external runtime configuration under the same
configuration version; copying the hash onto unreviewed runtime settings is not approval.
Changing public deployment configuration or migrations intentionally blocks automatic rollback.
A schema transition needs a separate reviewed compatibility/restore plan, not a down-migration.

## Build (no deployment or GitHub release)

After explicit build/package publication approval, dispatch `release-build.yml` on main.
Supply `upstream_images_json`, a public JSON object containing ONLY `POSTGRES_IMAGE`,
`TEMPORAL_IMAGE`, `CADDY_IMAGE`, `OTEL_COLLECTOR_IMAGE`, and `PROMETHEUS_IMAGE`, each a reviewed
non-placeholder digest reference. Supply `previous_run_id` from the accepted release-build
artifact, except during bootstrap.

The job requires successful Quality on the exact commit and a clean checkout. Only a
`git archive` of that commit becomes Docker's context: no local files, `.git`, environment
files, registry credentials or runtime secrets. Build arguments contain only the platform
image digest. Python tools/actions and base images are pinned; Git uses a fixed official Debian
snapshot (normal archive signature verification remains enabled). Tool output is not forwarded
by the Python runner. The archived source is secret-scanned. Each image runs bounded, read-only, network-denied
smoke checks before pushing: platform imports, provider worker/Git/Docker CLI availability,
and sandbox Python/pytest/Ruff without product code or Git/Docker. Secret and vulnerability
scans run **before** pushing. The immutable registry reference is scanned again and gets an
image-bound, nonempty SBOM, signature and attestation. Only a complete
verified candidate is uploaded as `signed-release-candidate`; failures do not upload evidence.
Unsigned intermediate uploads are not deployable releases. Commit-labelled transport tags may
be mutable; only signed digests are consumed. Automatic Docker build provenance and SBOM
are explicitly disabled; only the credential-free scanned reports are attached. Every CLI
workflow installs the frozen project environment rather than relying on host Python packages.

No build has been run by this implementation session. Linux image build, apt snapshot availability,
scanner databases, vulnerability disposition, Sigstore storage compatibility and GHCR permissions
must be proved in CI before using a candidate. Scanners cannot prove the absence of every possible
secret; source review, credential-free contexts and allowlisted COPY instructions remain required.

## Preflight (fail closed)

Download the complete verified candidate to an absolute directory and add these public values
to the external deployment environment:

```text
RELEASE_DIRECTORY=/opt/pr-reliability/releases/approved-candidate
RELEASE_COMMIT=<40-character approved commit>
DEPLOYMENT_CONFIG_VERSION=<configuration SHA-256 from signed release.json>
```

Copy Compose image references from that signed manifest. Every configured check must use its
signed `SANDBOX_IMAGE`; arbitrary extra tool images are rejected in this release format. Run:

```bash
uv run python -m infra.deployment.preflight /etc/pr-reliability/deployment.env
```

Preflight now requires the manifest/bundle/reports, verifies every owned image and SBOM
attestation, compares migration/configuration bytes to the checkout, and compares every Compose
image plus check image to the signed release. Missing, forged, placeholder or mismatched inputs
fail. Existing deployment environment examples still intentionally fail acceptance. A standalone
syntax-only Compose validation is not deployment preflight and does not prove signatures.

## Staging and publication (separate approvals)

Do not dispatch either workflow without the user's separate authorization. Both authorization
inputs default to false. No production deployment workflow is provided.

`release-staging.yml` selects the candidate/previous build run IDs and checks the candidate is
the exact current main workflow commit. After explicit disposable staging approval it verifies
both signed releases and requires identical migration sets and public configuration versions.
It materializes private candidate/rollback environment copies from the approved base file,
using only signed images. It then:

1. Deploys candidate with Compose `--wait`; checks private TLS/API/telemetry health and real E2E.
2. Quiesces writers and backs up all three databases with existing checksummed backup tooling.
3. Restores those dumps **onto the same disposable VM**, then reruns health and durable-data E2E.
4. Deploys the signed previous compatible release, then reruns health and E2E.
5. Re-promotes candidate, then reruns health and E2E.

Only after all steps pass is `staging.json` written and signed by the exact staging workflow
identity/commit. It records release/rollback hashes, backup-manifest checksum, E2E-program
checksum, workflow run ID, UTC completion time and all nine checks. No dump, environment,
credential, raw E2E output or private source is attached. This same-VM drill is not proof of a
fresh-host disaster recovery or a production restore; those remain separate operations acceptance.
Any failure stops with **no success receipt**. The VM may be left at that phase; inspect/recover
it through the approved operations runbook. Do not infer an automatic successful recovery.

After independent acceptance and separate GitHub release publication authorization, dispatch
`release-publication.yml` with candidate, previous and staging run IDs. The gate re-verifies
all signatures, exact release hashes, the approved E2E executable checksum, all required passing
checks, rollback compatibility and a completion time within 48 hours (future evidence fails).
It rejects existing tags instead of replacing history. Publication first creates a **draft**
with manifests, scans, SBOMs, signed staging evidence and a ZIP of the previous release. It reads
back the tag/attachments and checks every byte before making the release public. On failure the
draft stays unpublished; a human must inspect it. The token has no OIDC or package-write scope.

The CLI has matching `build`, `verify`, `stage`, `gate` and `publish` commands:
`uv run python -m infra.release --help`. Direct `stage`/`publish` also require explicit flags.

## Acceptance state

Code-ready evidence: strict manifest/preflight, build ordering, signature-policy arguments,
negative regression tests, staging orchestration, release gates and draft/read-back publication
are implemented. Synthetic verifier stubs test fail-closed plumbing, **not** real signatures.

Full issue-ready evidence: **absent**. Still required are an integrated approved commit,
independent review and Linux CI, three real scanned/published/SBOM-bearing/signed images, actual
signature verification and credential-exclusion evidence, an accepted previous release, the
approved Linux test infrastructure/E2E executable, exercised staging health/E2E/backup/restore/
rollback, signed real receipts and separate authorization for GitHub release publication.
The future PR must reference #45, not close it, until those facts are independently checked.

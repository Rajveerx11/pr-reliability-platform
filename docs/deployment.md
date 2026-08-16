# Private single-VM deployment

This runbook implements the repository-controlled part of issue
[#15](https://github.com/Rajveerx11/pr-reliability-platform/issues/15) and
[DEC-013](../plan/v1.md#dec-013--deploy-to-one-cloud-vm-after-local-validation). It is stacked on
`codex/issue-14-evaluation` because deployment depends on the evaluation and observability work.
It does not claim that a VM, certificate, backup restore, or test-repository review exists.

## Boundary

One Linux VM runs PostgreSQL, Temporal, the API, workers, OpenTelemetry Collector, Prometheus, and
Caddy. Only Caddy publishes a private-IP port. Prometheus binds to loopback for an SSH tunnel.
PostgreSQL, Temporal, OTLP, and application ports stay on an internal Docker network. The activity
worker alone has outbound network access for reviewed providers and GitHub. Its sandbox socket must
belong to a dedicated rootless Docker engine; never use the host root Docker socket.
The worker and rootless engine share only
`/run/user/UID/pr-reliability-sandbox-staging`, allowing the engine to mount staged source. The
worker removes abandoned sandbox and Proof temporary directories before every start; the host
runtime directory disappears at reboot.

TLS terminates at Caddy with a certificate issued for `PRIVATE_HOSTNAME` by the organization's
private CA. The VM firewall must allow TCP 443 only from the approved VPN or private CIDR and SSH
only from the administration network. Do not expose TCP 80, 5432, 7233, 8000, 8889, or 9090.

All images must use real repository digests. The shipped environment template intentionally fails
preflight until every example registry and repeated placeholder digest is replaced. Preflight
rejects tags, public bind addresses, database credentials that differ from the external password
file, tracked secret files, symlinks, missing files, broad secret permissions, and non-rootless
sandbox paths.

## Provision secrets outside source

On the VM, create a root-owned deployment directory on encrypted storage:

```text
sudo install -d -m 700 /etc/pr-reliability/secrets /var/backups/pr-reliability
sudo install -m 600 infra/deployment/deployment.env.example /etc/pr-reliability/deployment.env
sudo install -o 1001 -g 1001 -m 770 -d /run/user/1001/pr-reliability-sandbox-staging
```

Replace every example value in `/etc/pr-reliability/deployment.env`. Put the TLS certificate, TLS
key, private CA certificate, PostgreSQL password, and GitHub App private key at the absolute paths
named there. Put the same URL-escaped PostgreSQL password in `DATABASE_URL`; preflight verifies it
against `POSTGRES_PASSWORD_FILE`. The value remains outside source, but trusted Docker
administrators can inspect container environments.

Keep the environment file at mode `0600`. Create a dedicated secret-reader group, set its numeric
ID as `DEPLOYMENT_SECRET_GID`, assign secret files to that group, and use mode `0640`; the TLS
certificate and CA may use `0644`. Compose adds only that supplementary group to secret-consuming
containers. Provider keys and the webhook secret remain only in the external environment file. Docker
daemon administrators can inspect container environments and are trusted deployment operators.
Never copy this directory into a build context, backup bundle, support archive, or repository.
Set the staging directory group and `SANDBOX_ENGINE_GID` to the dedicated rootless engine group.
Compose runs the activity worker as the configured rootless engine UID and GID, so its mode-0700
temporary source directories remain traversable by that same host engine identity. Confirm the
identity can read the approved activity image, write only that staging directory, and connect to
only that socket.

## Preflight and deploy

Run from a clean checkout of the approved commit:

```text
python -m infra.deployment.preflight /etc/pr-reliability/deployment.env
docker compose --env-file /etc/pr-reliability/deployment.env --file infra/deployment/compose.vm.yaml config --quiet
docker compose --env-file /etc/pr-reliability/deployment.env --file infra/deployment/compose.vm.yaml pull
docker compose --env-file /etc/pr-reliability/deployment.env --file infra/deployment/compose.vm.yaml up -d
python -m infra.deployment.health --env-file /etc/pr-reliability/deployment.env
```

The one-shot `migrate` service applies checksummed migrations before the API and database-backed
workers start. A migration failure keeps dependants stopped. Confirm the certificate and hostname
from an approved private-network client, not only from the VM:

```text
curl --fail --cacert /path/to/private-ca.crt https://reviews.internal.example/health/ready
```

Use an SSH tunnel for Prometheus: `ssh -L 9090:127.0.0.1:9090 VM`, then open
`http://127.0.0.1:9090`. The collector-down alert is a local signal only until an approved private
alert receiver is configured. Check container restart counts, disk use, certificate expiry,
Prometheus readiness, API readiness, and failed publish audit events daily.

## Backup and restore drill

The backup command records which database writers are running or restarting, stops all writers,
dumps the application and both Temporal databases, writes SHA-256 checksums, then restarts only
that observed subset. Intentionally stopped publishing and worker services remain stopped. Keep
the destination outside the checkout on an encrypted volume and copy completed bundles to a second
encrypted location with restricted access.
Backup and restore share a non-blocking host lock next to the external deployment environment.
Concurrent manual or scheduled operations fail before changing service state.

```text
python -m infra.deployment.database --env-file /etc/pr-reliability/deployment.env backup /var/backups/pr-reliability
```

After one manual backup succeeds, install the reviewed systemd service and timer from
`infra/deployment/`, adjust `WorkingDirectory` only if the approved checkout uses another fixed
path, then enable the timer. Inspect `systemctl list-timers pr-reliability-backup.timer` and the
latest service result. Alert on any failed unit. The timer does not replace the off-VM encrypted
copy or recovery drill.

Restore is destructive. It verifies the complete manifest before stopping writers. Restore only
onto the intended disposable recovery VM first:

```text
python -m infra.deployment.database --env-file /etc/pr-reliability/deployment.env restore /var/backups/pr-reliability/BUNDLE --confirm restore-pr-reliability-v1
python -m infra.deployment.health --env-file /etc/pr-reliability/deployment.env
```

A successful restore returns only writers that were initially running to service. Lock, manifest,
or stop failures leave or return writers to their observed state. A failure after destructive
database restoration begins leaves all writers stopped so an operator can inspect partial recovery
before restarting intended services.

For acceptance, create a sentinel repository, run, approval, workflow, and audit record on a test
VM; back up; change the sentinel; restore into a fresh VM; verify all three databases and the API;
then save the timestamp, bundle checksum, commands, duration, and safe record IDs. Repository unit
tests prove command ordering, coverage, checksum rejection, and restart behavior. They do not prove
a real VM restore.

## End-to-end acceptance

Use a dedicated private test repository and test GitHub App installation. Deliver a signed webhook,
wait for analysis and sandbox verification, approve one finding, and confirm exactly one review is
published for the reviewed commit. Save safe run, finding, approval, review, trace, and commit IDs.
Do not use production repositories or credentials. This remains blocked until an operator supplies
the VM, private DNS and certificate, provider account, GitHub App, and test repository.

## Rollback

1. Stop new webhook delivery at the private load boundary.
2. Record current commit and image digests. Create and verify a backup bundle.
3. Replace application image digests in the external environment file with the last approved
   digests. Keep database, Temporal, Caddy, and monitoring digests unchanged unless they caused the
   incident.
4. Run preflight, pull, `up -d`, and the health command.
5. Re-enable webhooks only after readiness and one test-repository run pass.
6. If the newer release applied a backward-incompatible migration, restore its pre-deploy backup
   on a recovery VM instead of running ad-hoc reverse SQL. Verify it before replacing the failed VM.

If an unauthorized write occurred, keep publishing disabled, preserve safe audit IDs, rotate the
affected GitHub credentials, and follow the incident rule in [security.md](security.md).

## Acceptance status

- Repository config enforces private binding and TLS inputs: implemented and unit-tested.
- Secrets stay outside source control: implemented and unit-tested.
- Backup and restore tooling: implemented and unit-tested with fake commands; real VM drill blocked.
- Health and local monitoring: implemented; real VM observation blocked.
- End-to-end test-repository review: blocked by missing VM and external test authority.
- Rollback: documented; real rollback drill blocked.

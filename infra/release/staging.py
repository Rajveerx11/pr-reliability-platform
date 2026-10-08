"""Exercise one disposable Linux staging VM; never infer success from uploaded claims."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path

from infra.deployment.database import RESTORE_CONFIRMATION, backup, restore
from infra.deployment.health import check_health
from infra.deployment.preflight import validate_environment

from .compose import checked, database_runner, execute, validate_rendered_deployment
from .manifest import ReleaseError, digest, require_compatible, write_json
from .snapshot import snapshot_files, snapshot_release
from .verify import verify_release

E2E_PROGRAM = Path("/opt/pr-reliability/staging-e2e")
CHECKS = (
    "health",
    "end_to_end",
    "backup",
    "restore",
    "restored_end_to_end",
    "rollback_health",
    "rollback_end_to_end",
    "promoted_health",
    "promoted_end_to_end",
)


def exercise_staging(
    repository: Path,
    candidate_directory: Path,
    previous_directory: Path,
    candidate_env: Path,
    previous_env: Path,
    output: Path,
    run_id: str,
    e2e_program_sha256: str,
    *,
    authorized: bool = False,
) -> dict:
    if not authorized or platform.system() != "Linux":
        raise ReleaseError("staging requires explicit authorization on a disposable Linux VM")
    if not run_id.isdigit() or int(run_id) < 1 or output.exists():
        raise ReleaseError("staging requires a workflow run ID and a new evidence path")
    if not E2E_PROGRAM.is_file() or E2E_PROGRAM.is_symlink():
        raise ReleaseError("approved real-provider staging E2E program is not installed")
    with (
        snapshot_release(candidate_directory) as candidate_snapshot,
        snapshot_release(previous_directory) as previous_snapshot,
        snapshot_files(candidate_env.parent, (candidate_env.name,)) as candidate_env_snapshot,
        snapshot_files(previous_env.parent, (previous_env.name,)) as previous_env_snapshot,
    ):

        def unchanged() -> None:
            for snapshot in (
                candidate_snapshot,
                previous_snapshot,
                candidate_env_snapshot,
                previous_env_snapshot,
            ):
                snapshot.unchanged()

        return _exercise_staging(
            repository,
            candidate_directory,
            previous_directory,
            candidate_env_snapshot.directory / candidate_env.name,
            previous_env_snapshot.directory / previous_env.name,
            output,
            run_id,
            e2e_program_sha256,
            unchanged,
            candidate_snapshot.hashes["release.json"],
            previous_snapshot.hashes["release.json"],
        )


def _exercise_staging(
    repository: Path,
    candidate_directory: Path,
    previous_directory: Path,
    candidate_env: Path,
    previous_env: Path,
    output: Path,
    run_id: str,
    e2e_program_sha256: str,
    unchanged,
    candidate_sha: str,
    previous_sha: str,
) -> dict:
    candidate = verify_release(candidate_directory, repository=repository)
    previous = verify_release(previous_directory, repository=repository)
    require_compatible(candidate, previous, previous_sha)
    candidate_values = validate_environment(repository, candidate_env)
    previous_values = validate_environment(repository, previous_env)
    if (
        Path(candidate_values["RELEASE_DIRECTORY"]).resolve() != candidate_directory.resolve()
        or Path(previous_values["RELEASE_DIRECTORY"]).resolve() != previous_directory.resolve()
    ):
        raise ReleaseError("staging environments must reference the selected signed releases")
    # Both configurations address the SAME disposable VM and database, not production.
    stable_keys = (
        "PRIVATE_BASE_URL",
        "PRIVATE_BIND_ADDRESS",
        "DATABASE_URL",
        "BACKUP_DIRECTORY",
        "SANDBOX_DOCKER_SOCKET",
        "SANDBOX_STAGING_DIRECTORY",
    )
    if any(candidate_values[key] != previous_values[key] for key in stable_keys):
        raise ReleaseError("candidate and rollback must use the same staging infrastructure")
    compose = repository / "infra/deployment/compose.vm.yaml"
    program_sha = digest(E2E_PROGRAM)
    if program_sha != e2e_program_sha256:
        raise ReleaseError("staging E2E program does not match its approved checksum")
    completed: dict[str, str] = {}

    def values_for(env: Path) -> dict[str, str]:
        return candidate_values if env == candidate_env else previous_values

    def deploy(env: Path) -> None:
        unchanged()
        validate_rendered_deployment(
            repository, values_for(env), candidate if env == candidate_env else previous
        )
        checked(
            [
                "docker",
                "compose",
                "--env-file",
                str(env),
                "--file",
                str(compose),
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "300",
            ],
            values_for(env),
        )

    def health(env: Path, name: str) -> None:
        check_health(
            compose,
            env,
            command_runner=lambda command: execute(command, values_for(env), text=True),
        )
        completed[name] = "passed"

    def e2e(env: Path, name: str) -> None:
        # Credentials stay in external files; never serialize command output or secrets.
        checked([str(E2E_PROGRAM), "--env-file", str(env)], values_for(env))
        if digest(E2E_PROGRAM) != program_sha:
            raise ReleaseError("staging E2E program changed during execution")
        completed[name] = "passed"

    deploy(candidate_env)
    health(candidate_env, "health")
    e2e(candidate_env, "end_to_end")
    bundle = backup(
        repository,
        compose,
        candidate_env,
        Path(candidate_values["BACKUP_DIRECTORY"]),
        runner=database_runner(candidate_values),
    )
    completed["backup"] = "passed"
    backup_sha = digest(bundle / "manifest.json")
    restore(
        repository,
        compose,
        candidate_env,
        bundle,
        RESTORE_CONFIRMATION,
        runner=database_runner(candidate_values),
    )
    health(candidate_env, "restore")
    e2e(candidate_env, "restored_end_to_end")
    deploy(previous_env)
    health(previous_env, "rollback_health")
    e2e(previous_env, "rollback_end_to_end")
    deploy(candidate_env)
    health(candidate_env, "promoted_health")
    e2e(candidate_env, "promoted_end_to_end")
    unchanged()
    receipt = {
        "schema_version": 1,
        "environment": "linux-staging",
        "commit": candidate["commit"],
        "manifest_sha256": candidate_sha,
        "previous_manifest_sha256": previous_sha,
        "run_id": run_id,
        "completed_at": datetime.now(UTC).isoformat(),
        "checks": completed,
        "backup_manifest_sha256": backup_sha,
        "e2e_program_sha256": program_sha,
    }
    write_json(output, receipt)
    return receipt

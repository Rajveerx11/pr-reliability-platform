"""Release CLI. Remote publication and staging are never implicit."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from infra.deployment.preflight import load_environment

from .build import build
from .gate import release_gate
from .manifest import ReleaseError
from .staging import exercise_staging
from .verify import verify_release


def staging_environment(base: Path, directory: Path, destination: Path, repository: Path) -> Path:
    manifest = verify_release(directory, repository=repository)
    values = load_environment(base)
    if values.get("DEPLOYMENT_CONFIG_VERSION") != manifest["config_version"]:
        raise ReleaseError("staging runtime configuration version must already be approved")
    values.update(
        {name: image for name, image in manifest["images"].items() if name != "SANDBOX_IMAGE"}
    )
    values.update(RELEASE_DIRECTORY=str(directory.resolve()), RELEASE_COMMIT=manifest["commit"])
    # Only the operator's approved sandbox commands remain; substitute the signed tool image.
    import json

    checks = json.loads(values["REVIEW_CHECK_ALLOWLIST_JSON"])
    for check in checks:
        check["image"] = manifest["images"]["SANDBOX_IMAGE"]
    values["REVIEW_CHECK_ALLOWLIST_JSON"] = json.dumps(checks, separators=(",", ":"))
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("\n".join(f"{name}={value}" for name, value in values.items()) + "\n")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--repository", type=Path, default=Path.cwd())
    build_parser.add_argument("--commit", required=True)
    build_parser.add_argument("--upstream-images", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--previous", type=Path)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("directory", type=Path)
    verify_parser.add_argument("--repository", type=Path)
    stage = sub.add_parser("stage")
    stage.add_argument("--repository", type=Path, default=Path.cwd())
    stage.add_argument("--candidate", type=Path, required=True)
    stage.add_argument("--previous", type=Path, required=True)
    stage.add_argument("--base-env-file", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--run-id", required=True)
    stage.add_argument("--e2e-program-sha256", required=True)
    stage.add_argument("--authorize-disposable-staging", action="store_true")
    gate = sub.add_parser("gate")
    gate.add_argument("--candidate", type=Path, required=True)
    gate.add_argument("--previous", type=Path, required=True)
    gate.add_argument("--receipt", type=Path, required=True)
    gate.add_argument("--e2e-program-sha256", required=True)
    publication = sub.add_parser("publish")
    publication.add_argument("--candidate", type=Path, required=True)
    publication.add_argument("--previous", type=Path, required=True)
    publication.add_argument("--receipt", type=Path, required=True)
    publication.add_argument("--e2e-program-sha256", required=True)
    publication.add_argument("--authorize-github-release-publication", action="store_true")
    args = parser.parse_args()
    if args.operation == "build":
        build(args.repository, args.commit, args.upstream_images, args.output, args.previous)
    elif args.operation == "verify":
        verify_release(args.directory, repository=args.repository)
    elif args.operation == "stage":
        if not args.authorize_disposable_staging:
            raise ReleaseError("explicit disposable staging authorization is required")
        # Copies with runtime credentials live outside the checkout and never become artifacts.
        with tempfile.TemporaryDirectory() as temporary:
            candidate_env = staging_environment(
                args.base_env_file,
                args.candidate,
                Path(temporary) / "candidate.env",
                args.repository,
            )
            previous_env = staging_environment(
                args.base_env_file, args.previous, Path(temporary) / "previous.env", args.repository
            )
            exercise_staging(
                args.repository,
                args.candidate,
                args.previous,
                candidate_env,
                previous_env,
                args.output,
                args.run_id,
                args.e2e_program_sha256,
                authorized=True,
            )
    elif args.operation == "gate":
        release_gate(args.candidate, args.previous, args.receipt, args.e2e_program_sha256)
    else:
        from .publish import publish

        publish(
            args.candidate,
            args.previous,
            args.receipt,
            args.e2e_program_sha256,
            authorized=args.authorize_github_release_publication,
        )
    print("release checks passed")


if __name__ == "__main__":
    main()

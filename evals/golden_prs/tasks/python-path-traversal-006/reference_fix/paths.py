from pathlib import Path


def safe_artifact_path(root, requested):
    root_path = Path(root).resolve()
    target = (root_path / requested).resolve()
    if not target.is_relative_to(root_path):
        raise ValueError("path escapes root")
    return target

"""Resolve direct local Python imports without executing repository code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class DependencyResolution:
    """Resolved dependencies plus candidates omitted because resolution tied."""

    dependencies: frozenset[str]
    ambiguous: frozenset[str]


def direct_python_dependencies(
    path: str, content: str, repository_paths: set[str]
) -> DependencyResolution:
    """Return deterministic local targets imported directly by one Python file."""

    if not path.endswith(".py"):
        return DependencyResolution(frozenset(), frozenset())
    try:
        tree = ast.parse(content, filename=path)
    except (SyntaxError, ValueError):
        return DependencyResolution(frozenset(), frozenset())

    dependencies: set[str] = set()
    ambiguous: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target, tied = _resolve_absolute(path, alias.name, repository_paths)
                if target is not None:
                    dependencies.add(target)
                ambiguous.update(tied)
        elif isinstance(node, ast.ImportFrom):
            resolved, tied = _resolve_from_import(path, node, repository_paths)
            dependencies.update(resolved)
            ambiguous.update(tied)
    dependencies.discard(path)
    ambiguous.difference_update(dependencies)
    return DependencyResolution(frozenset(dependencies), frozenset(ambiguous))


def _resolve_from_import(
    current_path: str, node: ast.ImportFrom, repository_paths: set[str]
) -> tuple[set[str], set[str]]:
    targets: set[str] = set()
    ambiguous: set[str] = set()
    aliases = sorted(alias.name for alias in node.names if alias.name != "*")

    if node.level:
        directory = PurePosixPath(current_path).parent
        for _ in range(node.level - 1):
            directory = directory.parent
        base = directory.joinpath(*(node.module or "").split("."))
        candidates = [base]
        candidates.extend(base / alias for alias in aliases)
        for candidate in candidates:
            target = _resolve_path(candidate, repository_paths)
            if target is not None:
                targets.add(target)
        return targets, ambiguous

    if node.module is None:
        return targets, ambiguous
    modules = [node.module]
    modules.extend(f"{node.module}.{alias}" for alias in aliases)
    for module in modules:
        target, tied = _resolve_absolute(current_path, module, repository_paths)
        if target is not None:
            targets.add(target)
        ambiguous.update(tied)
    return targets, ambiguous


def _resolve_absolute(
    current_path: str, module: str, repository_paths: set[str]
) -> tuple[str | None, set[str]]:
    relative = module.replace(".", "/")
    suffixes = (f"{relative}.py", f"{relative}/__init__.py")
    matches = [
        path
        for path in repository_paths
        if any(path == suffix or path.endswith(f"/{suffix}") for suffix in suffixes)
    ]
    if not matches:
        return None, set()
    current_directory = PurePosixPath(current_path).parent.parts

    def rank(candidate: str) -> tuple[int, int]:
        candidate_directory = PurePosixPath(candidate).parent.parts
        shared = 0
        for current_part, candidate_part in zip(current_directory, candidate_directory):
            if current_part != candidate_part:
                break
            shared += 1
        distance = len(current_directory) + len(candidate_directory) - (2 * shared)
        return (-shared, distance)

    ranked = sorted((rank(candidate), candidate) for candidate in matches)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        best_rank = ranked[0][0]
        return None, {
            candidate for candidate_rank, candidate in ranked if candidate_rank == best_rank
        }
    return ranked[0][1], set()


def _resolve_path(path: PurePosixPath, repository_paths: set[str]) -> str | None:
    base = path.as_posix().strip("./")
    candidates = (f"{base}.py", f"{base}/__init__.py")
    for candidate in candidates:
        if candidate in repository_paths:
            return candidate
    return None

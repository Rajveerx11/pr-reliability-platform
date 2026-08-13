"""Select changed files before direct dependencies under a hard budget."""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable, Iterable, Mapping

from .models import ExcludedFile, SelectedContext, SelectedFile, SelectionSource
from .python_dependencies import direct_python_dependencies

TokenCounter = Callable[[str], int]

DEFAULT_EXCLUDED_PATTERNS = (
    ".git/**",
    "**/.git/**",
    "node_modules/**",
    "**/node_modules/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    "dist/**",
    "**/dist/**",
    "build/**",
    "**/build/**",
)


def estimate_tokens(text: str) -> int:
    """Return a deterministic conservative estimate for model-independent planning."""

    if not text:
        return 0
    return (len(text.encode("utf-8")) + 2) // 3


def select_context(
    files: Mapping[str, str],
    changed_paths: Iterable[str],
    token_budget: int,
    *,
    token_counter: TokenCounter = estimate_tokens,
    excluded_patterns: tuple[str, ...] = DEFAULT_EXCLUDED_PATTERNS,
) -> SelectedContext:
    """Prioritize changed files, then their direct local Python dependencies."""

    if token_budget < 1:
        raise ValueError("token_budget must be positive")

    repository = _normalize_repository(files)
    changed = sorted({_normalize_path(path) for path in changed_paths})
    exclusions: list[ExcludedFile] = []
    eligible_changed: list[str] = []

    for path in changed:
        reason = _ineligible_reason(path, repository, excluded_patterns)
        if reason is None:
            eligible_changed.append(path)
        else:
            exclusions.append(ExcludedFile(path, SelectionSource.CHANGED, reason))

    dependencies: set[str] = set()
    ambiguous_dependencies: set[str] = set()
    repository_paths = set(repository)
    for path in eligible_changed:
        resolution = direct_python_dependencies(path, repository[path], repository_paths)
        dependencies.update(resolution.dependencies)
        ambiguous_dependencies.update(resolution.ambiguous)
    dependencies.difference_update(eligible_changed)
    ambiguous_dependencies.difference_update(eligible_changed)

    for path in sorted(ambiguous_dependencies):
        exclusions.append(
            ExcludedFile(path, SelectionSource.DIRECT_DEPENDENCY, "ambiguous_dependency")
        )

    eligible_dependencies: list[str] = []
    for path in sorted(dependencies):
        reason = _ineligible_reason(path, repository, excluded_patterns)
        if reason is None:
            eligible_dependencies.append(path)
        else:
            exclusions.append(ExcludedFile(path, SelectionSource.DIRECT_DEPENDENCY, reason))

    ordered = [
        *((path, SelectionSource.CHANGED) for path in eligible_changed),
        *((path, SelectionSource.DIRECT_DEPENDENCY) for path in eligible_dependencies),
    ]
    selected: list[SelectedFile] = []
    rendered_parts: list[str] = []

    for path, source in ordered:
        before = "".join(rendered_parts)
        content = repository[path]
        complete = _render_file(path, source, content, truncated=False)
        complete_total = token_counter(before + complete)
        original_tokens = max(0, complete_total - token_counter(before))
        if complete_total <= token_budget:
            rendered_parts.append(complete)
            selected.append(
                SelectedFile(path, content, source, original_tokens, original_tokens, False)
            )
            continue

        prefix = _largest_prefix(before, path, source, content, token_budget, token_counter)
        if not prefix:
            exclusions.append(ExcludedFile(path, source, "budget_exhausted"))
            continue

        partial = _render_file(path, source, prefix, truncated=True)
        partial_total = token_counter(before + partial)
        rendered_parts.append(partial)
        selected.append(
            SelectedFile(
                path,
                prefix,
                source,
                original_tokens,
                max(0, partial_total - token_counter(before)),
                True,
            )
        )

    rendered = "".join(rendered_parts)
    return SelectedContext(
        files=tuple(selected),
        excluded=tuple(exclusions),
        rendered=rendered,
        total_tokens=token_counter(rendered),
        token_budget=token_budget,
    )


def _normalize_repository(files: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_path, content in files.items():
        if not isinstance(content, str):
            raise TypeError(f"file content must be text: {raw_path}")
        path = _normalize_path(raw_path)
        if path in normalized:
            raise ValueError(f"duplicate normalized path: {path}")
        normalized[path] = content
    return normalized


def _normalize_path(path: str) -> str:
    if not isinstance(path, str):
        raise TypeError("file path must be text")
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.endswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe repository path: {path}")
    if len(parts[0]) == 2 and parts[0][1] == ":":
        raise ValueError(f"unsafe repository path: {path}")
    return normalized


def _ineligible_reason(
    path: str, repository: Mapping[str, str], patterns: tuple[str, ...]
) -> str | None:
    if path not in repository:
        return "missing"
    if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
        return "excluded_pattern"
    return None


def _render_file(path: str, source: SelectionSource, content: str, *, truncated: bool) -> str:
    record = {
        "path": path,
        "source": source.value,
        "truncated": truncated,
        "content": content,
    }
    return json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"


def _largest_prefix(
    before: str,
    path: str,
    source: SelectionSource,
    content: str,
    token_budget: int,
    token_counter: TokenCounter,
) -> str:
    low = 0
    high = len(content)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _render_file(path, source, content[:middle], truncated=True)
        if token_counter(before + candidate) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return content[:low]

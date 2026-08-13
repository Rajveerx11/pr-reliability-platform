"""Results produced by the context selector."""

from dataclasses import dataclass
from enum import StrEnum


class SelectionSource(StrEnum):
    """Why a file was eligible for model context."""

    CHANGED = "changed"
    DIRECT_DEPENDENCY = "direct_dependency"


@dataclass(frozen=True, slots=True)
class SelectedFile:
    """One complete or budget-truncated file included in context."""

    path: str
    content: str
    source: SelectionSource
    original_tokens: int
    included_tokens: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ExcludedFile:
    """One eligible path omitted from context with a stable reason."""

    path: str
    source: SelectionSource
    reason: str


@dataclass(frozen=True, slots=True)
class SelectedContext:
    """A deterministic selection that never exceeds its token budget."""

    files: tuple[SelectedFile, ...]
    excluded: tuple[ExcludedFile, ...]
    rendered: str
    total_tokens: int
    token_budget: int

    def __post_init__(self) -> None:
        if self.total_tokens > self.token_budget:
            raise ValueError("selected context exceeds token budget")

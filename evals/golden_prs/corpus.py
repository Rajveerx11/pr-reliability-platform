"""Load and verify the frozen version-one golden PR corpus."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORPUS_ROOT = Path(__file__).resolve().parent
TASKS_ROOT = CORPUS_ROOT / "tasks"
TASK_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "title",
        "language",
        "category",
        "difficulty",
        "instruction",
        "known_defects",
        "allowed_findings",
        "fixture",
        "reference_fix",
        "verifier",
        "protected_paths",
    }
)
DIFFICULTIES = frozenset({"easy", "medium", "hard"})


class CorpusError(ValueError):
    """Raised when a golden task does not match the frozen contract."""


@dataclass(frozen=True)
class GoldenTask:
    id: str
    title: str
    language: str
    category: str
    difficulty: str
    instruction: str
    known_defects: tuple[str, ...]
    allowed_findings: tuple[str, ...]
    fixture: Path
    reference_fix: Path
    verifier: Path
    protected_paths: tuple[str, ...]


def load_corpus(root: Path = TASKS_ROOT) -> tuple[GoldenTask, ...]:
    """Load tasks in stable ID order and reject ambiguous files."""

    if not root.is_dir() or root.is_symlink():
        raise CorpusError("tasks root must be a real directory")
    task_dirs = sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    linked_dirs = sorted(
        path.name for path in root.iterdir() if path.is_dir() and path.is_symlink()
    )
    if linked_dirs:
        raise CorpusError(f"tasks root contains symlinked directories: {', '.join(linked_dirs)}")
    stray = sorted(path.name for path in root.iterdir() if not path.is_dir())
    if stray:
        raise CorpusError(f"tasks root contains files: {', '.join(stray)}")
    tasks = tuple(_load_task(path) for path in task_dirs)
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise CorpusError("task IDs must be unique")
    if ids != sorted(ids):
        raise CorpusError("task directory order must match task ID order")
    return tasks


def corpus_fingerprint(tasks: tuple[GoldenTask, ...]) -> str:
    """Hash metadata plus every fixture, fix, and verifier byte in stable order."""

    digest = hashlib.sha256()
    if not tasks:
        raise CorpusError("corpus must contain at least one task")
    support = _support_file(tasks[0])
    digest.update(support.name.encode())
    digest.update(b"\0")
    digest.update(support.read_bytes())
    digest.update(b"\0")
    for task in tasks:
        digest.update(task.id.encode())
        for path in _task_files(task):
            digest.update(path.relative_to(task.verifier.parent).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def verify_task(
    task: GoldenTask, *, fixed: bool, timeout_seconds: int = 10
) -> subprocess.CompletedProcess:
    """Run a protected verifier against the broken fixture or reference fix."""

    if not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be from 1 to 60")
    with tempfile.TemporaryDirectory(prefix=f"golden-{task.id}-") as temp:
        workspace = Path(temp) / "workspace"
        shutil.copytree(task.fixture, workspace)
        if fixed:
            shutil.copytree(task.reference_fix, workspace, dirs_exist_ok=True)
        return verify_workspace(task, workspace, timeout_seconds=timeout_seconds)


def verify_workspace(
    task: GoldenTask, workspace: Path, *, timeout_seconds: int = 10
) -> subprocess.CompletedProcess:
    """Run a protected verifier without importing candidate code into its process."""

    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("workspace must be a real directory")
    tampering = _candidate_tampering(workspace)
    if tampering:
        return subprocess.CompletedProcess(
            [sys.executable, "-I", str(task.verifier), str(workspace)],
            1,
            "",
            f"candidate tampering rejected: {tampering}",
        )
    protected = {path: path.read_bytes() for path in _protected_files(task)}
    result = subprocess.run(
        [sys.executable, "-I", str(task.verifier), str(workspace)],
        cwd=task.verifier.parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    changed = []
    for path, before in protected.items():
        try:
            unchanged = path.read_bytes() == before
        except OSError:
            unchanged = False
        if not unchanged:
            changed.append(path)
    if not changed:
        return result
    names = ", ".join(path.name for path in changed)
    return subprocess.CompletedProcess(
        result.args,
        1,
        result.stdout,
        f"{result.stderr}\nprotected verifier changed: {names}".strip(),
    )


def _load_task(task_dir: Path) -> GoldenTask:
    manifest = task_dir / "task.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read {manifest}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != TASK_KEYS:
        raise CorpusError(f"{manifest}: fields must be exactly {', '.join(sorted(TASK_KEYS))}")
    if data["schema_version"] != 1:
        raise CorpusError(f"{manifest}: schema_version must be 1")
    task_id = _text(data["id"], "id")
    if task_dir.name != task_id:
        raise CorpusError(f"{manifest}: directory name must equal id")
    difficulty = _text(data["difficulty"], "difficulty")
    if difficulty not in DIFFICULTIES:
        raise CorpusError(f"{manifest}: unsupported difficulty")
    fixture = _directory(task_dir, data["fixture"], "fixture")
    reference_fix = _directory(task_dir, data["reference_fix"], "reference_fix")
    verifier = _file(task_dir, data["verifier"], "verifier")
    protected_paths = _text_list(data["protected_paths"], "protected_paths")
    if Path(data["verifier"]).as_posix() not in protected_paths:
        raise CorpusError(f"{manifest}: verifier must be protected")
    for protected in protected_paths:
        _file(task_dir, protected, "protected_paths item")
    _reject_symlinks(task_dir)
    return GoldenTask(
        id=task_id,
        title=_text(data["title"], "title"),
        language=_text(data["language"], "language"),
        category=_text(data["category"], "category"),
        difficulty=difficulty,
        instruction=_text(data["instruction"], "instruction"),
        known_defects=_text_list(data["known_defects"], "known_defects"),
        allowed_findings=_text_list(data["allowed_findings"], "allowed_findings"),
        fixture=fixture,
        reference_fix=reference_fix,
        verifier=verifier,
        protected_paths=protected_paths,
    )


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusError(f"{name} must be non-empty text")
    return value.strip()


def _text_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CorpusError(f"{name} must be a non-empty list")
    parsed = tuple(_text(item, f"{name} item") for item in value)
    if len(parsed) != len(set(parsed)):
        raise CorpusError(f"{name} items must be unique")
    return parsed


def _safe_child(root: Path, value: Any, name: str) -> Path:
    relative = Path(_text(value, name))
    if relative.is_absolute() or ".." in relative.parts:
        raise CorpusError(f"{name} must stay inside task directory")
    target = root / relative
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise CorpusError(f"{name} escapes task directory") from exc
    return target


def _directory(root: Path, value: Any, name: str) -> Path:
    path = _safe_child(root, value, name)
    if not path.is_dir() or path.is_symlink():
        raise CorpusError(f"{name} must be a real directory")
    return path


def _file(root: Path, value: Any, name: str) -> Path:
    path = _safe_child(root, value, name)
    if not path.is_file() or path.is_symlink():
        raise CorpusError(f"{name} must be a real file")
    return path


def _reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CorpusError(f"task contains symlink: {path.relative_to(root)}")


def _task_files(task: GoldenTask) -> tuple[Path, ...]:
    root = task.verifier.parent
    paths = [root / "task.json", task.verifier]
    paths.extend(path for path in task.fixture.rglob("*") if _is_corpus_file(path))
    paths.extend(path for path in task.reference_fix.rglob("*") if _is_corpus_file(path))
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def _protected_files(task: GoldenTask) -> tuple[Path, ...]:
    task_files = tuple(task.verifier.parent / relative for relative in task.protected_paths)
    return (*task_files, _support_file(task))


def _support_file(task: GoldenTask) -> Path:
    support = task.verifier.parent.parents[1] / "verifier_support.py"
    if not support.is_file() or support.is_symlink():
        raise CorpusError("verifier support must be a real file beside tasks directory")
    return support


def _is_corpus_file(path: Path) -> bool:
    """Ignore interpreter caches that do not belong to the frozen source corpus."""

    return path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"


def _candidate_tampering(workspace: Path) -> str | None:
    """Reject direct process-exit and frame-introspection verifier bypasses."""

    blocked_modules = {"glob", "inspect", "tempfile"}
    blocked_names = {"SystemExit", "exit", "quit"}
    blocked_attributes = {
        ("inspect", "currentframe"),
        ("os", "_exit"),
        ("os", "scandir"),
        ("os", "walk"),
        ("pathlib", "Path.glob"),
        ("pathlib", "Path.iterdir"),
        ("pathlib", "Path.rglob"),
        ("sys", "_getframe"),
        ("sys", "exit"),
    }
    for path in sorted(workspace.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            return f"cannot inspect {path.relative_to(workspace)}: {exc}"
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name in blocked_modules:
                        return f"{path.relative_to(workspace)} imports {imported.name}"
                    aliases[imported.asname or imported.name] = imported.name
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                for imported in node.names:
                    if node.module in blocked_modules or imported.name in blocked_names:
                        return (
                            f"{path.relative_to(workspace)} imports {node.module}.{imported.name}"
                        )
                    if (node.module, imported.name) in blocked_attributes:
                        return (
                            f"{path.relative_to(workspace)} imports {node.module}.{imported.name}"
                        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Call, ast.Name)):
                raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if isinstance(raised, ast.Name) and raised.id == "SystemExit":
                    return f"{path.relative_to(workspace)} raises SystemExit"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in blocked_names:
                    return f"{path.relative_to(workspace)} calls {node.func.id}"
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            symbol = (aliases.get(node.value.id, node.value.id), node.attr)
            if symbol in blocked_attributes:
                return f"{path.relative_to(workspace)} uses {symbol[0]}.{symbol[1]}"
            if symbol[0] in {"Path", "pathlib.Path"} and symbol[1] in {"glob", "iterdir", "rglob"}:
                return f"{path.relative_to(workspace)} uses Path.{symbol[1]}"
    return None

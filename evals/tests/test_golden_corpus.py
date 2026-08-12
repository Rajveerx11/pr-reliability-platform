import json
import shutil
from pathlib import Path

import pytest

from evals.golden_prs import (
    CorpusError,
    corpus_fingerprint,
    load_corpus,
    verify_task,
    verify_workspace,
)
from evals.golden_prs.corpus import CORPUS_ROOT, TASKS_ROOT


def test_version_one_corpus_has_ten_stable_tasks() -> None:
    tasks = load_corpus()

    assert len(tasks) == 10
    assert [task.id for task in tasks] == sorted(task.id for task in tasks)
    assert all(task.known_defects for task in tasks)
    assert all(task.allowed_findings for task in tasks)
    assert all(task.verifier.name == "verifier.py" for task in tasks)


@pytest.mark.parametrize("task", load_corpus(), ids=lambda task: task.id)
def test_verifier_rejects_broken_and_accepts_reference_fix(task) -> None:
    broken = verify_task(task, fixed=False)
    fixed = verify_task(task, fixed=True)

    assert broken.returncode != 0, broken.stdout + broken.stderr
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr


def test_corpus_matches_frozen_fingerprint() -> None:
    expected = (TASKS_ROOT.parent / "corpus.sha256").read_text(encoding="ascii").strip()

    assert corpus_fingerprint(load_corpus()) == expected


def test_interpreter_caches_do_not_change_fingerprint(tmp_path: Path) -> None:
    copied_root = tmp_path / "golden_prs"
    shutil.copytree(CORPUS_ROOT, copied_root)
    tasks_root = copied_root / "tasks"
    before = corpus_fingerprint(load_corpus(tasks_root))
    cache = next(tasks_root.glob("*/fixture")) / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "generated.pyc").write_bytes(b"not corpus source")

    assert corpus_fingerprint(load_corpus(tasks_root)) == before


def test_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    target = tmp_path / "tasks"
    shutil.copytree(TASKS_ROOT, target)
    manifest = next(target.glob("*/task.json"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["hidden_override"] = True
    manifest.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CorpusError, match="fields must be exactly"):
        load_corpus(target)


def test_manifest_rejects_path_escape(tmp_path: Path) -> None:
    target = tmp_path / "tasks"
    shutil.copytree(TASKS_ROOT, target)
    manifest = next(target.glob("*/task.json"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["fixture"] = "../outside"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CorpusError, match="must stay inside"):
        load_corpus(target)


def test_task_directory_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "tasks"
    target.mkdir()
    real_task = next(TASKS_ROOT.iterdir())
    try:
        (target / real_task.name).symlink_to(real_task, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(CorpusError, match="symlinked directories"):
        load_corpus(target)


def test_candidate_exit_zero_cannot_bypass_verifier(tmp_path: Path) -> None:
    task = next(task for task in load_corpus() if task.id == "python-idempotency-001")
    workspace = tmp_path / "workspace"
    shutil.copytree(task.fixture, workspace)
    (workspace / "checkout.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")

    result = verify_workspace(task, workspace)

    assert result.returncode != 0


def test_candidate_forged_stdout_cannot_bypass_verifier(tmp_path: Path) -> None:
    task = next(task for task in load_corpus() if task.id == "python-idempotency-001")
    workspace = tmp_path / "workspace"
    shutil.copytree(task.fixture, workspace)
    attack = 'import os\nprint(\'{"same":true,"charges":[2500]}\', flush=True)\nos._exit(0)\n'
    (workspace / "checkout.py").write_text(attack, encoding="utf-8")

    result = verify_workspace(task, workspace)

    assert result.returncode != 0


def test_candidate_frame_inspection_cannot_forge_result(tmp_path: Path) -> None:
    task = next(task for task in load_corpus() if task.id == "python-idempotency-001")
    workspace = tmp_path / "workspace"
    shutil.copytree(task.fixture, workspace)
    attack = (
        "import inspect\n"
        "frame = inspect.currentframe()\n"
        "while frame:\n"
        "    result_path = frame.f_globals.get('result_path')\n"
        "    if result_path:\n"
        "        open(result_path, 'w').write('{\"same\":true,\"charges\":[2500]}')\n"
        "        break\n"
        "    frame = frame.f_back\n"
    )
    (workspace / "checkout.py").write_text(attack, encoding="utf-8")

    result = verify_workspace(task, workspace)

    assert result.returncode != 0
    assert "candidate tampering rejected" in result.stderr


def test_candidate_system_exit_and_temp_scan_cannot_forge_result(tmp_path: Path) -> None:
    task = next(task for task in load_corpus() if task.id == "python-idempotency-001")
    workspace = tmp_path / "workspace"
    shutil.copytree(task.fixture, workspace)
    attack = (
        "import json\n"
        "import tempfile\n"
        "from pathlib import Path\n"
        "for folder in Path(tempfile.gettempdir()).glob('golden-result-*'):\n"
        '    (folder / \'result.json\').write_text(json.dumps({"same": True, "charges": [2500]}))\n'
        "raise SystemExit(0)\n"
    )
    (workspace / "checkout.py").write_text(attack, encoding="utf-8")

    result = verify_workspace(task, workspace)

    assert result.returncode != 0
    assert "candidate tampering rejected" in result.stderr


def test_candidate_verifier_mutation_is_detected(tmp_path: Path) -> None:
    copied_root = tmp_path / "golden_prs"
    shutil.copytree(CORPUS_ROOT, copied_root)
    task = next(
        task for task in load_corpus(copied_root / "tasks") if task.id == "python-idempotency-001"
    )
    workspace = tmp_path / "workspace"
    shutil.copytree(task.fixture, workspace)
    attack = f"from pathlib import Path\nPath({str(task.verifier)!r}).write_text('changed')\n"
    (workspace / "checkout.py").write_text(attack, encoding="utf-8")

    result = verify_workspace(task, workspace)

    assert result.returncode != 0
    assert "protected verifier changed" in result.stderr

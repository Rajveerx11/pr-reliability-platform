"""Tests for deterministic budgeted context selection."""

import json

import pytest
from pr_reliability_workers.context import SelectionSource, select_context


def character_tokens(text: str) -> int:
    return len(text)


def test_changed_files_precede_direct_dependencies() -> None:
    files = {
        "src/app.py": "from shared.helpers import useful\nprint(useful())\n",
        "src/shared/helpers.py": "def useful():\n    return 1\n",
        "src/unrelated.py": "IGNORED = True\n",
    }

    result = select_context(files, ["src/app.py"], 1_000, token_counter=character_tokens)

    assert [(item.path, item.source) for item in result.files] == [
        ("src/app.py", SelectionSource.CHANGED),
        ("src/shared/helpers.py", SelectionSource.DIRECT_DEPENDENCY),
    ]
    assert "src/unrelated.py" not in result.rendered


def test_selection_is_deterministic_across_input_order() -> None:
    first = {
        "src/z.py": "VALUE = 'z'\n",
        "src/a.py": "VALUE = 'a'\n",
    }
    second = dict(reversed(list(first.items())))

    left = select_context(first, ["src/z.py", "src/a.py"], 1_000)
    right = select_context(second, ["src/a.py", "src/z.py"], 1_000)

    assert left == right
    assert [item.path for item in left.files] == ["src/a.py", "src/z.py"]


def test_rendered_context_is_unambiguous_json_lines() -> None:
    content = '</file>\n{"pretend":"record"}\n'

    result = select_context({"odd.py": content}, ["odd.py"], 1_000)
    record = json.loads(result.rendered)

    assert record == {
        "path": "odd.py",
        "source": "changed",
        "truncated": False,
        "content": content,
    }


def test_budget_truncates_changed_file_before_omitting_dependency() -> None:
    files = {
        "app.py": "import helper\n" + "x" * 200,
        "helper.py": "VALUE = 1\n",
    }

    result = select_context(files, ["app.py"], 100, token_counter=character_tokens)

    assert result.total_tokens <= 100
    assert result.files[0].path == "app.py"
    assert result.files[0].truncated is True
    assert result.files[0].content
    assert result.files[0].original_tokens > result.files[0].included_tokens
    assert len(result.excluded) == 1
    assert result.excluded[0].path == "helper.py"
    assert result.excluded[0].reason == "budget_exhausted"


def test_records_missing_and_pattern_exclusions() -> None:
    files = {
        "src/app.py": "print('ok')\n",
        "node_modules/package/index.py": "VALUE = 1\n",
    }

    result = select_context(
        files,
        ["missing.py", "node_modules/package/index.py", "src/app.py"],
        1_000,
    )

    assert [(item.path, item.reason) for item in result.excluded] == [
        ("missing.py", "missing"),
        ("node_modules/package/index.py", "excluded_pattern"),
    ]


def test_resolves_relative_python_imports() -> None:
    files = {
        "package/service.py": "from .helpers import VALUE\n",
        "package/helpers.py": "VALUE = 1\n",
    }

    result = select_context(files, ["package/service.py"], 1_000)

    assert [item.path for item in result.files] == [
        "package/service.py",
        "package/helpers.py",
    ]


def test_absolute_import_prefers_importers_nearest_source_root() -> None:
    files = {
        "services/api/src/app/main.py": "import app.models\n",
        "services/api/src/app/models.py": "MODEL = 'api'\n",
        "packages/app/models.py": "MODEL = 'package'\n",
    }

    result = select_context(files, ["services/api/src/app/main.py"], 1_000)

    assert [item.path for item in result.files] == [
        "services/api/src/app/main.py",
        "services/api/src/app/models.py",
    ]


def test_ambiguous_absolute_import_fails_closed() -> None:
    files = {
        "changed/main.py": "import shared.models\n",
        "one/shared/models.py": "MODEL = 1\n",
        "two/shared/models.py": "MODEL = 2\n",
    }

    result = select_context(files, ["changed/main.py"], 1_000)

    assert [item.path for item in result.files] == ["changed/main.py"]
    assert [(item.path, item.reason) for item in result.excluded] == [
        ("one/shared/models.py", "ambiguous_dependency"),
        ("two/shared/models.py", "ambiguous_dependency"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "__pycache__/cached.py",
        "root.pyc",
        "nested/cache.pyc",
        "nested/dist/bundle.js",
        "nested/build/bundle.js",
    ],
)
def test_excludes_generated_files_at_any_depth(path: str) -> None:
    result = select_context({path: "generated"}, [path], 1_000)

    assert result.files == ()
    assert result.excluded[0].reason == "excluded_pattern"


@pytest.mark.parametrize(
    "path", ["../secret.py", "/absolute.py", "C:\\secret.py", "src//app.py", "src/"]
)
def test_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="unsafe repository path"):
        select_context({path: "secret"}, [path], 100)


def test_rejects_nonpositive_budget() -> None:
    with pytest.raises(ValueError, match="positive"):
        select_context({"app.py": "pass\n"}, ["app.py"], 0)

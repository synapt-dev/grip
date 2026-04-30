from __future__ import annotations

import ast
from pathlib import Path

import pytest


def test_python_driver_unions_imports_and_overlay_wins_conflicting_function_body(
    tmp_path: Path,
) -> None:
    from gr2_overlay.language_drivers import merge_python_overlay

    ancestor = tmp_path / "ancestor.py"
    current = tmp_path / "current.py"
    other = tmp_path / "other.py"

    ancestor.write_text("def build_message() -> str:\n    return 'ancestor'\n")
    current.write_text(
        "import os\n\n"
        "def build_message() -> str:\n"
        "    return 'base'\n"
    )
    other.write_text(
        "from pathlib import Path\n\n"
        "def build_message() -> str:\n"
        "    return 'overlay'\n"
    )

    merge_python_overlay(
        ancestor=ancestor,
        current=current,
        other=other,
        relative_path="app/main.py",
    )

    merged = current.read_text()
    module = ast.parse(merged)
    namespace: dict[str, object] = {}
    exec(compile(module, filename="app/main.py", mode="exec"), namespace)

    assert "import os" in merged
    assert "from pathlib import Path" in merged
    assert namespace["build_message"]() == "overlay"


def test_python_driver_raises_explicit_composition_conflict_for_non_mergeable_symbol_edit(
    tmp_path: Path,
) -> None:
    from gr2_overlay.language_drivers import (
        PythonCompositionConflict,
        merge_python_overlay,
    )

    ancestor = tmp_path / "ancestor.py"
    current = tmp_path / "current.py"
    other = tmp_path / "other.py"

    ancestor.write_text("TIMEOUT = 10\n")
    current.write_text("TIMEOUT = 30\n")
    other.write_text("TIMEOUT = 60\n")

    before = current.read_text()

    with pytest.raises(PythonCompositionConflict) as exc:
        merge_python_overlay(
            ancestor=ancestor,
            current=current,
            other=other,
            relative_path="app/settings.py",
        )

    assert exc.value.error_code == "composition_conflict"
    assert current.read_text() == before


def test_python_driver_refuses_non_python_paths(tmp_path: Path) -> None:
    from gr2_overlay.language_drivers import merge_python_overlay

    ancestor = tmp_path / "ancestor.txt"
    current = tmp_path / "current.txt"
    other = tmp_path / "other.txt"

    ancestor.write_text("ancestor\n")
    current.write_text("base\n")
    other.write_text("overlay\n")

    with pytest.raises(ValueError, match="Python driver only supports .py paths"):
        merge_python_overlay(
            ancestor=ancestor,
            current=current,
            other=other,
            relative_path="settings.toml",
        )

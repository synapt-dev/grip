"""Contract: shipped modules import `gr2.python_cli`, never a bare `python_cli`.

Three call-time imports used the unqualified package name --
`config.py` twice and `grip.py` once -- against 200-plus qualified ones
elsewhere.  `gr2 grip snapshot` and `gr2 config restore` answered with
`ModuleNotFoundError: No module named 'python_cli'`.  The logic underneath was
sound: with `gr2/` also on `sys.path` both exit 0 and produce a real snapshot.

WHY NOTHING CAUGHT IT.  `test_gr2_packaging.py` already contains a test whose
whole job is importability without conftest injection, and it PASSES.  It
cannot see these, because they are imports *inside function bodies*, executed
only when the function runs.  An import-time check walks the module's closure
at import time and a call-time import is not in it.

So this assertion parses the SOURCE rather than importing it: an `ast` walk
sees every `ImportFrom` node, whatever scope it sits in.  That is the whole
difference between this check and the one that already existed and passed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SHIPPED = "python_cli"


def _shipped_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return sorted((root / "gr2" / "python_cli").rglob("*.py"))


def _bare_imports(path: Path) -> list[str]:
    """Every `from python_cli...` / `import python_cli` in the file, at any depth."""
    found = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == SHIPPED or mod.startswith(SHIPPED + "."):
                found.append(f"{path.name}:{node.lineno}: from {mod} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SHIPPED or alias.name.startswith(SHIPPED + "."):
                    found.append(f"{path.name}:{node.lineno}: import {alias.name}")
    return found


def test_no_shipped_module_imports_a_bare_python_cli():
    offenders = [hit for f in _shipped_files() for hit in _bare_imports(f)]
    assert offenders == [], "bare python_cli imports: " + "; ".join(offenders)


def test_the_scan_finds_a_call_time_import(tmp_path):
    """The control, and it is the ONLY thing that makes the test above mean
    anything.

    The check that already existed passed while three of these were live, so
    an empty result here is worthless unless the scanner is shown to catch the
    exact shape that defeated the previous one: an import nested inside a
    function body, which no import-time walk reaches.
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f():\n"
        "    from python_cli.gitops import repo_dirty\n"
        "    return repo_dirty\n"
    )
    hits = _bare_imports(sample)
    assert len(hits) == 1, f"scanner missed a call-time import: {hits}"
    assert "sample.py:2" in hits[0]


def test_the_scan_does_not_flag_the_qualified_form(tmp_path):
    """The negative control: `gr2.python_cli` must not match, or the check
    would flag all 200-plus correct imports and be turned off within a day."""
    sample = tmp_path / "ok.py"
    sample.write_text(
        "def f():\n"
        "    from gr2.python_cli.gitops import repo_dirty\n"
        "    return repo_dirty\n"
    )
    assert _bare_imports(sample) == []


@pytest.mark.parametrize("verb_module", ["gr2.python_cli.config", "gr2.python_cli.grip"])
def test_the_repaired_modules_import_cleanly(verb_module):
    """Fruit, not only source analysis: the two modules that carried the bare
    imports must import with only the package root on the path."""
    import importlib

    assert importlib.import_module(verb_module) is not None

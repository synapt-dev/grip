"""``scripts/text_guards.py --check`` passes only when the text guards ran and passed.

Each row runs the script as a child, the way the freeze does, against a copy of
the gr2 tree so a planted fault never touches the real one. A red guard, an
empty collection, an import failure and a missing pytest all exit non-zero;
only a clean run exits 0; and the guards see the CI environment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

GR2 = Path(__file__).resolve().parents[1]
SCRIPT = Path("scripts") / "text_guards.py"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "gr2"
    shutil.copytree(GR2, root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info"))
    return root


def _run(root: Path, python: str = sys.executable, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ACTIONS", "PYTHONPATH")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [python, str(root / SCRIPT), "--check", *args],
        cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,
    )


def test_a_clean_tree_passes():
    proc = _run(GR2)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "text guards: passed" in proc.stdout


def test_an_old_word_in_the_readme_fails_and_names_the_test(tmp_path):
    root = _copy(tmp_path)
    readme = root / "README.md"
    readme.write_text(readme.read_text() + "\nA review can stall (a bind fault).\n")
    proc = _run(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "test_readme_review_text_uses_the_new_words" in proc.stdout
    assert "text guards: text guards failed (pytest rc 1)" in proc.stdout


def test_the_guards_see_the_ci_environment(tmp_path):
    root = _copy(tmp_path)
    guard = root / "tests" / "test_vocabulary.py"
    guard.write_text(
        guard.read_text()
        + "\n\ndef test_env_row_sees_ci():\n"
        + "    import os\n"
        + "    assert os.environ.get('GITHUB_ACTIONS') == 'true'\n"
    )
    proc = _run(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "text guards: passed" in proc.stdout


def test_nothing_collected_is_not_a_pass(tmp_path):
    root = _copy(tmp_path)
    for path in (root / "tests").glob("test_*.py"):
        path.write_text("")
    proc = _run(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "no tests collected from the text guard files (pytest rc 5)" in proc.stdout


def test_an_import_failure_is_not_a_pass(tmp_path):
    root = _copy(tmp_path)
    guard = root / "tests" / "test_vocabulary.py"
    guard.write_text("import gr2_no_such_module_for_this_row\n" + guard.read_text())
    proc = _run(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not be collected" in proc.stdout


def test_an_interpreter_without_pytest_is_an_instrument_error(tmp_path):
    env_dir = tmp_path / "bare"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(env_dir)
    python = env_dir / "bin" / "python"
    probe = subprocess.run([str(python), "-c", "import pytest"], capture_output=True, stdin=subprocess.DEVNULL)
    if probe.returncode == 0:
        pytest.fail("the bare interpreter imports pytest; this row cannot hold")
    proc = _run(GR2, str(python))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pytest is not importable by" in proc.stdout
    assert "passed" not in proc.stdout


def test_a_touched_list_names_only_the_guard_files(tmp_path):
    proc = _run(GR2, sys.executable, "gr2/README.md", "gr2/tests/test_vocabulary.py", "other/tests/test_vocabulary.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "text guards: range touched tests/test_vocabulary.py\n" in proc.stdout

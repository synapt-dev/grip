"""Packaging tests: gr2 must be a real installable CLI, not a pytest-only import.

Today, gr2.python_cli and gr2.prototypes only resolve inside pytest, via the
root conftest.py's sys.modules injection (`gr2/conftest.py`). gr2/pyproject.toml
packages only gr2_overlay. There is no console_scripts entry point.

These tests deliberately run each check in a fresh subprocess with cwd pinned to
a neutral tmp_path (never gr2/ or its parent), so a pass can only mean the
package is genuinely installed and importable -- not that Python happened to
find these modules via a cwd-relative accident or pytest's own sys.modules hack,
which subprocesses don't inherit anyway.

grip#752 slice 1 (D3): pyproject at grip/gr2/... console_scripts entry point.
Reference path correction: the repo directory is gitgrip/gr2/, not
gitgrip/grip/gr2/ as CLAUDE.md's layout doc has it (flagged separately, #dev).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def test_gr2_python_cli_imports_without_conftest_injection(tmp_path: Path) -> None:
    """gr2.python_cli must be importable as a real package, not via sys.modules hack."""
    result = _run([sys.executable, "-c", "import gr2.python_cli.app"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_gr2_prototypes_imports_without_conftest_injection(tmp_path: Path) -> None:
    """gr2.prototypes -- the load-bearing import in app.py/syncops.py/execops.py -- must resolve for real."""
    result = _run(
        [sys.executable, "-c", "import gr2.prototypes.lane_workspace_prototype"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr


def test_gr2_prototypes_repo_maintenance_imports(tmp_path: Path) -> None:
    result = _run(
        [sys.executable, "-c", "import gr2.prototypes.repo_maintenance_prototype"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr


def test_gr2_overlay_still_imports_unprefixed(tmp_path: Path) -> None:
    """gr2_overlay stays its own top-level package -- the packaging fix must not break it."""
    result = _run([sys.executable, "-c", "import gr2_overlay"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_gr2_console_script_resolves(tmp_path: Path) -> None:
    """console_scripts entry point `gr2` must be on PATH and runnable after install."""
    result = _run(["gr2", "--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_gr2_module_invocation_works(tmp_path: Path) -> None:
    """python -m gr2.python_cli must also work (uses the existing __main__.py)."""
    result = _run([sys.executable, "-m", "gr2.python_cli", "--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr

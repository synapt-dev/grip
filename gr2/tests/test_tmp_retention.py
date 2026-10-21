"""A passed test leaves nothing under the basetemp.

The isolation fixture makes three dirs per test and every test gets a tmp_path; with pytest's
default retention ("all") each one stays until the session ends, so a full run holds a dir tree
per test at its peak. The inner run below uses the real fixture and the real retention setting
from gr2's pyproject, so it reads what the suite itself does.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

pytest_plugins = ["pytester"]

_GR2 = Path(__file__).resolve().parents[1]

_INNER_CONFTEST = f"""
import importlib.util

_spec = importlib.util.spec_from_file_location("_gr2_root_conftest", {str(_GR2 / "conftest.py")!r})
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_isolated_git_config = _mod._isolated_git_config
"""


def test_passed_tests_leave_no_tmp_dirs(pytester):
    ini = tomllib.loads((_GR2 / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    lines = ["[pytest]"]
    if "tmp_path_retention_policy" in ini:
        lines.append(f"tmp_path_retention_policy = {ini['tmp_path_retention_policy']}")
    pytester.makeini("\n".join(lines) + "\n")
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(
        test_two="""
        def test_one(tmp_path):
            (tmp_path / "f").write_text("x")

        def test_two(tmp_path):
            (tmp_path / "f").write_text("y")
        """
    )
    basetemp = pytester.path / "bt"
    result = pytester.runpytest_inprocess(f"--basetemp={basetemp}", "test_two.py")
    result.assert_outcomes(passed=2)

    left = sorted(p.name for p in basetemp.iterdir()) if basetemp.exists() else []
    assert [n for n in left if n.startswith("test_")] == []
    assert [n for n in left if n.startswith("isolated-")] == []

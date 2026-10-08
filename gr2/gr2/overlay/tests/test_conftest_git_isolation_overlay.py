"""Counter-witness that `_isolated_git_config` reaches this SIBLING testpath too.

`gr2/overlay/tests/` and `gr2/tests/` are both listed in `testpaths`
(pyproject.toml) but neither is an ancestor of the other, so an autouse
fixture placed in `gr2/tests/conftest.py` alone would never apply here --
pytest scopes a directory's conftest to that directory's own subtree, not to
a sibling. This file is the same env-vars witness as
`gr2/tests/test_conftest_git_isolation.py`, placed in the sibling tree on
purpose: it is the one thing a fixture living in the wrong conftest cannot
fake.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_isolated_git_config_env_vars_point_at_present_isolated_files_here_too(tmp_path_factory):
    """Same assertion as the `tests/` copy, run from the sibling `overlay/tests/`
    tree: the three isolation channels use present targets with fixture identity only,
    not merely unset. If `_isolated_git_config` lived only in `gr2/tests/conftest.py`
    (the bug this file exists to catch), every one of these would read as
    ``None``/absent here, because this directory is not a descendant of that
    conftest's scope."""
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"

    global_path = os.environ.get("GIT_CONFIG_GLOBAL")
    assert global_path, "GIT_CONFIG_GLOBAL must be set, not absent"
    p = Path(global_path)
    assert p.is_file(), f"GIT_CONFIG_GLOBAL must point at a real file: {global_path!r}"
    entries = subprocess.run(
        ["git", "config", "--file", str(p), "--list"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert entries == ["user.name=Fixture", "user.email=fixture@example.invalid"], (
        f"GIT_CONFIG_GLOBAL must contain only fixture identity: {entries!r}"
    )

    xdg_path = os.environ.get("XDG_CONFIG_HOME")
    assert xdg_path, "XDG_CONFIG_HOME must be set, not absent"
    xp = Path(xdg_path)
    assert xp.is_dir(), f"XDG_CONFIG_HOME must point at a real directory: {xdg_path!r}"
    ignore_file = xp / "git" / "ignore"
    assert not ignore_file.exists(), (
        f"XDG_CONFIG_HOME/git/ignore must not exist: {ignore_file}"
    )

"""Stamps each build with the commit it was built from; everything else is in pyproject.toml.

`gr2 --version` on a non-editable install has no checkout to ask, so the build writes what
it knows into the package as `gr2/python_cli/_build.json`:

* `commit` -- the short sha of `HEAD`, or null when git is absent or this is not a checkout
  that tracks this `setup.py` (an sdist unpacked inside another repo uses its carried stamp);
* `release` -- true only when `HEAD` carries the tag `gr2/v<version>`, the tag the release
  workflow builds from, AND no tracked file under this directory differs from `HEAD`;
* `dirty` -- true when a file under this directory differs from `HEAD`, tracked or not (an
  untracked module would ship in the wheel); only the build's own output (`build/`,
  `dist/`, `*.egg-info/`) is excluded, by pathspec, so no ignore file decides it.

An sdist carries the stamp its own build wrote, so a wheel built FROM the sdist (what
`python -m build` does, with no `.git` in the unpacked tree) keeps the source commit
instead of degrading to unknown. The version itself is never derived here: it stays the
static literal in pyproject.toml.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

_STAMP = os.path.join("gr2", "python_cli", "_build.json")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    # No inherited GIT_* (git exports GIT_DIR to hooks): git finds the repo from _HERE alone.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(
            [git, *args], cwd=_HERE, env=env, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _stamp(version: str) -> dict:
    """What this build is. In a checkout of this tree, read git live; else an sdist's carried stamp."""
    # git answers only when this tree's own setup.py is tracked: an sdist unpacked inside
    # some other checkout would otherwise be stamped with THAT repo's HEAD.
    tracked = _git("ls-files", "--error-unmatch", "setup.py") is not None
    commit = _git("rev-parse", "--short", "HEAD") if tracked else None
    if commit:
        tags = (_git("tag", "--points-at", "HEAD") or "").split()
        status = _git(
            "status", "--porcelain", "--untracked-files=all", "--", ".",
            ":(exclude)build", ":(exclude)dist", ":(exclude,glob)**/*.egg-info/**",
        )
        if status is None or status:
            return {"commit": commit, "dirty": True, "release": False}
        return {"commit": commit, "release": f"gr2/v{version}" in tags}
    carried = os.path.join(_HERE, _STAMP)
    if os.path.isfile(carried):
        with open(carried, encoding="utf-8") as fh:
            return json.load(fh)
    return {"commit": None, "release": False}


def _write(root: str, stamp: dict) -> None:
    target = os.path.join(root, _STAMP)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(stamp, fh)


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        # An editable install reads its checkout's HEAD live; a stamp there would go stale.
        if not getattr(self, "editable_mode", False):
            _write(self.build_lib, _stamp(self.distribution.get_version()))


class Sdist(sdist):
    def make_release_tree(self, base_dir, files) -> None:
        # Stamp first: setuptools builds the release tree inside this directory, untracked.
        stamp = _stamp(self.distribution.get_version())
        super().make_release_tree(base_dir, files)
        _write(base_dir, stamp)


setup(cmdclass={"build_py": BuildPy, "sdist": Sdist})

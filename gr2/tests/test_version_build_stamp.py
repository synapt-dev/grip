"""`gr2 --version` from an INSTALLED build says which build it is.

Each row builds a real wheel from a copy of this tree through the stock setuptools backend,
unpacks it as an install, and asks `version_line()` from that install with site-packages
disabled, so the editable checkout running the suite cannot answer in its place.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

_GR2 = Path(__file__).resolve().parents[1]
_VERSION = tomllib.loads((_GR2 / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]
_IDENT = ["-c", "user.email=fixture@example.com", "-c", "user.name=Fixture"]


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _source(tmp_path: Path) -> Path:
    """A committed copy of the distribution's sources in its own git repo."""
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "setup.py", "README.md"):
        if (_GR2 / name).exists():
            shutil.copy2(_GR2 / name, src / name)
    skip = shutil.ignore_patterns("__pycache__", "tests", "*.egg-info")
    for pkg in ("gr2", "gr2_overlay"):
        shutil.copytree(_GR2 / pkg, src / pkg, ignore=skip)
    _git(["-c", "init.defaultBranch=dev", "init", "-q"], src)
    _git([*_IDENT, "add", "-A"], src)
    _git([*_IDENT, "commit", "-q", "-m", "seed"], src)
    return src


def _installed_line(
    src: Path, tmp_path: Path, *, path_env: str | None = None, extra_env: dict[str, str] | None = None
) -> str:
    """Build a wheel from `src`, unpack it as an install, and return its version line."""
    out = tmp_path / "dist"
    out.mkdir()
    env = dict(os.environ)
    if path_env is not None:
        env["PATH"] = path_env
    env.update(extra_env or {})
    subprocess.run(
        [sys.executable, "-c", f"from setuptools import build_meta as b; b.build_wheel({str(out)!r})"],
        cwd=src,
        env=env,
        check=True,
        capture_output=True,
    )
    (wheel,) = out.glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)
    probe = (
        f"import sys; sys.path.insert(0, {str(site)!r}); "
        "from gr2.python_cli.version import version_line; print(version_line())"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def test_release_build_at_its_tag_says_released(tmp_path: Path) -> None:
    """Control: a build at the `gr2/v<version>` tag prints exactly what a release prints."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)

    assert _installed_line(src, tmp_path) == f"{_VERSION} released"


def test_dev_build_names_its_sha_and_is_not_a_release(tmp_path: Path) -> None:
    """A build from an untagged commit names that commit and says it is not a release."""
    src = _source(tmp_path)
    sha = _git(["rev-parse", "--short", "HEAD"], src)

    assert _installed_line(src, tmp_path) == f"{_VERSION}+g{sha} dev build, not a release"


def test_build_without_git_says_unknown_build_not_released(tmp_path: Path) -> None:
    """With no git on PATH at build time, the build says unknown, never a release name."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    nobin = tmp_path / "nobin"
    nobin.mkdir()

    assert _installed_line(src, tmp_path, path_env=str(nobin)) == (
        f"{_VERSION} unknown build, not a release"
    )


def test_tracked_edit_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """A build at the release tag with an uncommitted tracked edit says dirty, not released."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    with (src / "README.md").open("a", encoding="utf-8") as fh:
        fh.write("\nan edit the tag does not carry\n")

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_sdist_built_inside_another_checkout_keeps_its_carried_commit(tmp_path: Path) -> None:
    """An sdist unpacked inside some other repo names the source commit, never that repo's HEAD."""
    src = _source(tmp_path)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    sdists = tmp_path / "sdist"
    sdists.mkdir()
    subprocess.run(
        [sys.executable, "-c", f"from setuptools import build_meta as b; b.build_sdist({str(sdists)!r})"],
        cwd=src,
        check=True,
        capture_output=True,
    )
    (sdist,) = sdists.glob("*.tar.gz")
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "README").write_text("another project\n", encoding="utf-8")
    _git(["-c", "init.defaultBranch=main", "init", "-q"], outer)
    _git([*_IDENT, "add", "-A"], outer)
    _git([*_IDENT, "commit", "-q", "-m", "outer"], outer)
    outer_sha = _git(["rev-parse", "--short", "HEAD"], outer)
    with tarfile.open(sdist) as tf:
        tf.extractall(outer / "vendor", filter="data")
    (unpacked,) = (outer / "vendor").iterdir()
    carried = json.loads((unpacked / "gr2" / "python_cli" / "_build.json").read_text("utf-8"))
    assert carried["commit"] == sha != outer_sha

    assert _installed_line(unpacked, tmp_path) == f"{_VERSION}+g{sha} dev build, not a release"


@pytest.mark.parametrize("work_tree", [True, False], ids=["git-dir-and-work-tree", "git-dir-alone"])
def test_inherited_git_dir_never_points_the_stamp_at_another_repo(tmp_path: Path, work_tree: bool) -> None:
    """GIT_DIR (as git exports it to hooks) naming another repo that tracks setup.py is ignored."""
    src = _source(tmp_path)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    sdists = tmp_path / "sdist"
    sdists.mkdir()
    subprocess.run(
        [sys.executable, "-c", f"from setuptools import build_meta as b; b.build_sdist({str(sdists)!r})"],
        cwd=src,
        check=True,
        capture_output=True,
    )
    (sdist,) = sdists.glob("*.tar.gz")
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "setup.py").write_text("# another project\n", encoding="utf-8")
    _git(["-c", "init.defaultBranch=main", "init", "-q"], outer)
    _git([*_IDENT, "add", "-A"], outer)
    _git([*_IDENT, "commit", "-q", "-m", "outer"], outer)
    outer_sha = _git(["rev-parse", "--short", "HEAD"], outer)
    with tarfile.open(sdist) as tf:
        tf.extractall(tmp_path / "unpacked", filter="data")
    (unpacked,) = (tmp_path / "unpacked").iterdir()
    assert sha != outer_sha
    git_env = {"GIT_DIR": str(outer / ".git")}
    if work_tree:
        git_env["GIT_WORK_TREE"] = str(outer)

    assert _installed_line(unpacked, tmp_path, extra_env=git_env) == f"{_VERSION}+g{sha} dev build, not a release"

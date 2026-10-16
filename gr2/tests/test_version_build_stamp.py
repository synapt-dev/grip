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
import time
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


def test_untracked_module_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """An untracked module ships in the wheel, so a build at the tag with one says dirty."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    (src / "gr2" / "python_cli" / "zz_untracked.py").write_text("SHIPPED = True\n", encoding="utf-8")

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_ignored_module_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """A `.gitignore`d module ships in the wheel too, so a build at the tag with one says dirty."""
    src = _source(tmp_path)
    (src / ".gitignore").write_text("*_local.py\n", encoding="utf-8")
    _git([*_IDENT, "add", ".gitignore"], src)
    _git([*_IDENT, "commit", "-q", "-m", "ignore local modules"], src)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    (src / "gr2" / "python_cli" / "zz_local.py").write_text("SHIPPED = True\n", encoding="utf-8")

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_hidden_tracked_edit_at_the_tag_is_never_released(tmp_path: Path, flag: str) -> None:
    """A tracked module git is told not to look at still ships its edit, so the tag says dirty."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    _git(["update-index", flag, "gr2/python_cli/version.py"], src)
    module = src / "gr2" / "python_cli" / "version.py"
    module.write_text(module.read_text(encoding="utf-8") + "HIDDEN = True\n", encoding="utf-8")
    assert _git(["status", "--porcelain"], src) == ""

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def _converted_source(tmp_path: Path, attributes: str) -> Path:
    """A source whose `.gitattributes` is committed, then tagged at that commit."""
    src = _source(tmp_path)
    (src / ".gitattributes").write_text(attributes, encoding="utf-8")
    _git([*_IDENT, "add", "--renormalize", "."], src)
    _git([*_IDENT, "add", ".gitattributes"], src)
    _git([*_IDENT, "commit", "-q", "-m", "attributes"], src)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    return src


def _same_size_rewrite(path: Path, old: bytes, new: bytes) -> None:
    """Swap `old` for `new` in place, keeping the size so git trusts nothing but content."""
    assert len(old) == len(new)
    data = path.read_bytes()
    assert data.count(old) == 1
    path.write_bytes(data.replace(old, new))
    later = path.stat().st_mtime + 2
    os.utime(path, (later, later))


def test_clean_filter_edit_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """A clean filter maps a same-size edit back to the committed blob; the edit still ships."""
    src = _converted_source(tmp_path, "*.py filter=tilde\n")
    _git(["config", "filter.tilde.clean", "tr '~' ' '"], src)
    _git(["config", "filter.tilde.smudge", "cat"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    module = src / "gr2" / "python_cli" / "version.py"
    _same_size_rewrite(module, b"The distinction is", b"The~distinction is")
    assert _git(["status", "--porcelain"], src) == ""

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_ident_edit_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """`ident` collapses anything between `$Id:` and `$` on the way in, so code there ships unseen."""
    src = _source(tmp_path)
    module = src / "gr2" / "python_cli" / "version.py"
    module.write_text(module.read_text(encoding="utf-8") + 'ID = "$Id$"\n', encoding="utf-8")
    _git([*_IDENT, "add", "-A"], src)
    _git([*_IDENT, "commit", "-q", "-m", "ident line"], src)
    (src / ".gitattributes").write_text("*.py ident\n", encoding="utf-8")
    _git([*_IDENT, "add", ".gitattributes"], src)
    _git([*_IDENT, "commit", "-q", "-m", "attributes"], src)
    module.unlink()
    _git(["checkout", "--", "gr2/python_cli/version.py"], src)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    expanded = module.read_bytes().split(b'ID = "')[-1].split(b'"')[0]
    code = b'$Id: "+__import__("os").name+" $'
    pad = b" " * (len(expanded) - len(code))
    _same_size_rewrite(module, expanded, code.replace(b"$Id: ", b"$Id: " + pad))
    assert _git(["status", "--porcelain"], src) == ""

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_working_tree_encoding_edit_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """working-tree-encoding decodes both byte orders to one blob, so a swapped file ships unseen."""
    src = _source(tmp_path)
    data = src / "gr2" / "python_cli" / "notes.txt"
    data.write_bytes("release notes\n".encode("utf-16"))
    (src / ".gitattributes").write_text("*.txt working-tree-encoding=UTF-16\n", encoding="utf-8")
    _git([*_IDENT, "add", "-A"], src)
    _git([*_IDENT, "commit", "-q", "-m", "notes"], src)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    raw = data.read_bytes()
    text = raw.decode("utf-16")
    if raw[:2] == b"\xff\xfe":
        swapped = b"\xfe\xff" + text.encode("utf-16-be")
    else:
        swapped = b"\xff\xfe" + text.encode("utf-16-le")
    _same_size_rewrite(data, raw, swapped)
    assert _git(["status", "--porcelain"], src) == ""

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def _settled_same_size_edit(src: Path, config: list[str]) -> Path:
    """A same-size edit to a module with its mtime put back (cp -p, rsync -t), index settled first."""
    for pair in config:
        _git(["config", *pair.split("=", 1)], src)
    time.sleep(1.1)  # past the racy second, so the index entry is trusted on stat alone
    _git(["update-index", "--refresh"], src)
    module = src / "gr2" / "python_cli" / "version.py"
    before = module.stat()
    data = module.read_bytes()
    assert data.count(b"The distinction is") == 1
    module.write_bytes(data.replace(b"The distinction is", b"The~distinction is"))
    os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
    return module


@pytest.mark.parametrize("config", ["core.trustctime=false", "core.checkStat=minimal"])
def test_narrowed_stat_check_edit_at_the_tag_is_never_released(tmp_path: Path, config: str) -> None:
    """Repo config that narrows the stat check hides a same-size, mtime-restored edit from status."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    _settled_same_size_edit(src, [config])
    assert _git(["status", "--porcelain"], src) == ""

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_fsmonitor_hook_reporting_nothing_at_the_tag_is_never_released(tmp_path: Path) -> None:
    """An fsmonitor hook that reports no change makes status skip an edited file entirely."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    hook = tmp_path / "fsmonitor-nothing"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(["config", "core.fsmonitor", str(hook)], src)
    _git(["update-index", "--fsmonitor"], src)
    _git(["status", "--porcelain"], src)
    module = src / "gr2" / "python_cli" / "version.py"
    _same_size_rewrite(module, b"The distinction is", b"The~distinction is")
    assert _git(["status", "--porcelain"], src) == ""

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_settled_same_size_edit_under_default_config_is_dirty(tmp_path: Path) -> None:
    """Control: the same edit under default config moves ctime, so status itself sees it."""
    src = _source(tmp_path)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    sha = _git(["rev-parse", "--short", "HEAD"], src)
    _settled_same_size_edit(src, [])
    assert _git(["status", "--porcelain"], src) == "M gr2/python_cli/version.py"

    assert _installed_line(src, tmp_path) == (
        f"{_VERSION}+g{sha}.dirty dev build, not a release"
    )


def test_attribute_that_converts_nothing_is_still_released(tmp_path: Path) -> None:
    """Control: an attribute git never converts content with leaves the tag a release."""
    src = _converted_source(tmp_path, "*.py diff=python\n")

    assert _installed_line(src, tmp_path) == f"{_VERSION} released"


def test_pycache_alone_at_the_tag_is_still_released(tmp_path: Path) -> None:
    """Control: bytecode under `__pycache__/` is not a source change, so the tag still releases."""
    src = _source(tmp_path)
    (src / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git([*_IDENT, "add", ".gitignore"], src)
    _git([*_IDENT, "commit", "-q", "-m", "ignore bytecode"], src)
    _git([*_IDENT, "tag", f"gr2/v{_VERSION}"], src)
    cache = src / "gr2" / "python_cli" / "__pycache__"
    cache.mkdir()
    (cache / "version.cpython-314.pyc").write_bytes(b"\0")

    assert _installed_line(src, tmp_path) == f"{_VERSION} released"


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

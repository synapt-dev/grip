"""A repo cache refresh fetches the effective URL with the mirror's own refspec, never a remote NAME, so neither a
remote planted on the cache with an option-shaped name nor a `remotes.origin` group reaches git's argv; a cache
holding such keys is refused as corrupt, naming the key.

Every row that plants a group runs the refresh in a child process under a process cap (RLIMIT_NPROC) and a
timeout that kills its process group: a group naming origin itself makes git recurse without end."""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from gr2.python_cli import gitops

_GR2_ROOT = Path(__file__).resolve().parents[1]
_REFRESH = """
import sys
from pathlib import Path
from gr2.python_cli import gitops
if sys.argv[3] == "skip-refusal":
    gitops._refuse_planted_cache_remotes = lambda root: None
try:
    print("created" if gitops.ensure_repo_cache(sys.argv[1], Path(sys.argv[2])) else "refreshed")
except SystemExit as exc:
    print("exit", exc)
except Exception as exc:
    print(type(exc).__name__, exc)
"""


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _live_processes() -> int:
    # `ps` works on Linux and macOS alike (macOS has no /proc); RLIMIT_NPROC counts every process of this user on both.
    out = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid="], capture_output=True, text=True).stdout
    return len(out.split())


def _capped_refresh(src: Path, cache: Path, mode: str) -> str:
    """ensure_repo_cache in a child under RLIMIT_NPROC live+150 and a 60s timeout that kills the whole group."""
    cap = _live_processes() + 150

    def _preexec() -> None:
        resource.setrlimit(resource.RLIMIT_NPROC, (cap, cap))

    env = {**os.environ, "PYTHONPATH": str(_GR2_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen(
        [sys.executable, "-c", _REFRESH, str(src), str(cache), mode],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        preexec_fn=_preexec, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return "TIMEOUT"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return out.strip()


def _source_and_cache(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    _git("-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "one")
    cache = tmp_path / "cache" / "a.git"
    assert gitops.ensure_repo_cache(str(src), cache) is True
    return src, cache


def _new_commit(src: Path) -> str:
    _git("-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "two")
    return _git("-C", str(src), "rev-parse", "HEAD")


def _hostile(tmp_path: Path, src: Path, cache: Path) -> tuple[str, Path]:
    marker = tmp_path / "fired"
    script = tmp_path / "up.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    script.chmod(0o755)
    hostile = f"--upload-pack={script}"
    _git("--git-dir", str(cache), "config", f"remote.{hostile}.url", str(src))
    return hostile, marker


@pytest.mark.parametrize("group", ["origin {h}", "{h} origin", "origin origin", "{h}"])
def test_the_refresh_never_fetches_a_group_or_planted_remote(tmp_path: Path, group: str) -> None:
    """With the refusal bypassed, the fetch itself names only the URL: no member runs, origin's commits arrive."""
    src, cache = _source_and_cache(tmp_path)
    hostile, marker = _hostile(tmp_path, src, cache)
    _git("--git-dir", str(cache), "config", "remotes.origin", group.format(h=hostile))
    head = _new_commit(src)

    assert _capped_refresh(src, cache, "skip-refusal") == "refreshed"

    assert not marker.exists()
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == head


@pytest.mark.parametrize("plant", ["remote", "group-two", "group-self", "dotted"])
def test_a_cache_holding_a_group_or_another_remote_is_refused_naming_the_key(tmp_path: Path, plant: str) -> None:
    src, cache = _source_and_cache(tmp_path)
    if plant == "group-self":
        _git("--git-dir", str(cache), "config", "remotes.origin", "origin origin")
        key = "remotes.origin"
    elif plant == "dotted":  # a subsection that only begins with origin: remote."origin.x".url
        _git("--git-dir", str(cache), "config", "remote.origin.x.url", str(src))
        key = "remote.origin.x.url"
    else:
        hostile, marker = _hostile(tmp_path, src, cache)
        key = f"remote.{hostile}.url"
        if plant == "group-two":
            _git("--git-dir", str(cache), "config", "remotes.origin", f"origin {hostile}")

    out = _capped_refresh(src, cache, "full")

    assert out.startswith("GripCorruptError ")
    assert f"config key {key}" in out
    assert str(src) not in out
    assert not (tmp_path / "fired").exists()


def test_a_mirror_of_another_repo_at_the_cache_path_is_not_fetched_into(tmp_path: Path) -> None:
    """Same repo name, different repo: its refs stay as they were and this repo's commit never lands in it."""
    src, _ = _source_and_cache(tmp_path)
    head = _new_commit(src)
    other = tmp_path / "other"
    _git("init", "-q", "-b", "main", str(other))
    _git("-C", str(other), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    cache = tmp_path / "cache2" / "a.git"
    _git("clone", "-q", "--mirror", str(other), str(cache))
    before = _git("--git-dir", str(cache), "for-each-ref", "--format=%(refname) %(objectname)")

    assert gitops.ensure_repo_cache(str(src), cache) is False

    assert _git("--git-dir", str(cache), "for-each-ref", "--format=%(refname) %(objectname)") == before
    assert _git("--git-dir", str(cache), "remote", "get-url", "origin") == str(other)
    assert subprocess.run(["git", "--git-dir", str(cache), "cat-file", "-e", head], capture_output=True).returncode != 0


def test_the_refresh_still_brings_new_origin_commits_into_the_cache(tmp_path: Path) -> None:
    src, cache = _source_and_cache(tmp_path)
    head = _new_commit(src)

    assert gitops.ensure_repo_cache(str(src), cache) is False

    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == head

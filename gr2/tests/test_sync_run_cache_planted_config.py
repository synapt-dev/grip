"""A repo cache refresh must not run a command planted in the cache's own git config.

The planted key is `core.hooksPath`: it points git at a directory holding a `reference-transaction` hook, which
`git fetch` runs when it updates the mirror's refs. The remote-key refusal reads only `remote.*` and `remotes.*`,
so this key passes it and the refresh fetch runs the hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import gitops


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(src: Path, msg: str) -> None:
    _git("-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", msg)


def test_refresh_does_not_run_a_hook_planted_through_core_hookspath(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    _commit(src, "one")
    cache = tmp_path / "cache" / "a.git"
    assert gitops.ensure_repo_cache(str(src), cache) is True

    marker = tmp_path / "fired"
    hooks = tmp_path / "planted-hooks"
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat >/dev/null\n")
    hook.chmod(0o755)
    _git("--git-dir", str(cache), "config", "core.hooksPath", str(hooks))
    _commit(src, "two")

    refusal = ""
    try:
        gitops.ensure_repo_cache(str(src), cache)
    except (SystemExit, Exception) as exc:
        refusal = str(exc)

    assert not marker.exists(), "repo cache refresh ran a hook planted through core.hooksPath"
    assert "core.hooksPath" in refusal or "core.hookspath" in refusal, refusal
    assert str(hooks) not in refusal and "planted-hooks" not in refusal, refusal


def test_a_clean_cache_still_refreshes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    _commit(src, "one")
    cache = tmp_path / "cache" / "a.git"
    assert gitops.ensure_repo_cache(str(src), cache) is True
    _commit(src, "two")

    assert gitops.ensure_repo_cache(str(src), cache) is False
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == _git("-C", str(src), "rev-parse", "HEAD")


def _made_cache(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    _commit(src, "one")
    cache = tmp_path / "cache" / "a.git"
    assert gitops.ensure_repo_cache(str(src), cache) is True
    _commit(src, "two")
    return src, cache


def _refusal(src: Path, cache: Path) -> str:
    try:
        gitops.ensure_repo_cache(str(src), cache)
    except (SystemExit, Exception) as exc:
        return str(exc)
    return ""


def test_an_unknown_key_is_refused_by_name_not_value(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    _git("--git-dir", str(cache), "config", "foo.bar", "secretvalue1")
    refusal = _refusal(src, cache)
    assert "config key foo.bar" in refusal, refusal
    assert "secretvalue1" not in refusal, refusal


def test_a_cache_gr2_made_and_repointed_refreshes(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    keys = set(_git("--git-dir", str(cache), "config", "--local", "--list", "--name-only").splitlines())
    assert {"remote.origin.url", "remote.origin.fetch", "remote.origin.mirror"} <= keys, keys
    assert _refusal(src, cache) == ""
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == _git("-C", str(src), "rev-parse", "HEAD")


def test_include_path_is_refused_not_followed(tmp_path: Path) -> None:
    # `config --local --list` lists include.path itself and does not read the included file, so the allowlist
    # cannot judge what it holds: the include key is refused outright.
    src, cache = _made_cache(tmp_path)
    included = tmp_path / "included.cfg"
    included.write_text("[foo]\n\tbar = 1\n")
    _git("--git-dir", str(cache), "config", "include.path", str(included))
    listed = _git("--git-dir", str(cache), "config", "--local", "--list", "--name-only").splitlines()
    assert "include.path" in listed and "foo.bar" not in listed, listed
    refusal = _refusal(src, cache)
    assert "config key include.path" in refusal, refusal
    assert str(included) not in refusal, refusal

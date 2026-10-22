"""A repo cache refresh must not run a command planted in the cache's own git config.

The planted key is `core.hooksPath`: it points git at a directory holding a `reference-transaction` hook, which
`git fetch` runs when it updates the mirror's refs. The remote-key refusal reads only `remote.*` and `remotes.*`,
so this key passes it and the refresh fetch runs the hook."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import gitops


@pytest.fixture(autouse=True)
def _no_host_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host /etc/gitconfig or ~/.gitconfig that sets core.hooksPath would keep a cache hook from running even with
    # gr2's hooks-off flag removed, so the hook rows could not fail. Measure with git's own defaults.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")


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


def test_a_review_mirror_gr2_let_filter_refreshes(tmp_path: Path) -> None:
    # gr2 writes this key itself on every review mirror after ensuring it, so the next review of the repo ensures again.
    src, cache = _made_cache(tmp_path)
    _git("--git-dir", str(cache), "config", "uploadpack.allowFilter", "true")
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


def _hook(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat >/dev/null\n")
    path.chmod(0o755)


def _tip(git_dir: Path) -> str:
    return gitops.run_argv(["git", "--git-dir", str(git_dir), "rev-parse", "refs/heads/" + _branch(git_dir)]).stdout.strip()


def _branch(git_dir: Path) -> str:
    return gitops.run_argv(["git", "--git-dir", str(git_dir), "symbolic-ref", "--short", "HEAD"]).stdout.strip()


def test_a_hook_file_in_the_cache_does_not_run(tmp_path: Path) -> None:
    # No config key names this hook; git finds it at <cache>/hooks/reference-transaction on its own. gr2 runs git on
    # a cache with its hooks switched off, so the refresh goes through and the hook never fires.
    src, cache = _made_cache(tmp_path)
    marker = tmp_path / "fired"
    _hook(cache / "hooks" / "reference-transaction", marker)
    refusal = _refusal(src, cache)
    assert not marker.exists(), "repo cache refresh ran a hook file planted in the cache's hooks directory"
    assert refusal == "", refusal
    assert _tip(cache) == _tip(src / ".git"), "repo cache with a planted hook file was not refreshed"


def test_a_cache_with_no_hooks_dir_still_refreshes(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    shutil.rmtree(cache / "hooks", ignore_errors=True)
    assert _refusal(src, cache) == ""
    assert _tip(cache) == _tip(src / ".git"), "repo cache with no hooks directory was not refreshed"


def test_a_url_rewrite_for_the_origin_is_refused_by_name_not_value(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    _git("--git-dir", str(cache), "config", "url.https://rewrite.invalid/x.insteadOf", str(src))
    refusal = _refusal(src, cache)
    assert "config key url.https://rewrite.invalid/x.insteadof" in refusal, refusal
    assert str(src) not in refusal, refusal


def test_worktree_config_is_refused_by_name(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    marker = tmp_path / "fired"
    hooks = tmp_path / "planted-hooks"
    _hook(hooks / "reference-transaction", marker)
    _git("--git-dir", str(cache), "config", "extensions.worktreeConfig", "true")
    (cache / "config.worktree").write_text(f"[core]\n\thooksPath = {hooks}\n")
    refusal = _refusal(src, cache)
    assert not marker.exists(), "repo cache refresh ran a hook planted through config.worktree"
    assert "config key extensions.worktreeconfig" in refusal, refusal
    assert str(hooks) not in refusal, refusal


def _src_with_a_file(tmp_path: Path) -> Path:
    # A file, so a blobless clone of src really lacks a blob.
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    (src / "f.txt").write_text("one\n")
    _git("-C", str(src), "add", "f.txt")
    _commit(src, "one")
    return src


def _keys(cache: Path) -> list[str]:
    keys = _git("--git-dir", str(cache), "config", "--local", "--list", "--name-only").splitlines()
    print(f"{cache.name} keys: {keys}")
    return keys


def _seeded_from(src: Path, local_source: Path, tmp_path: Path) -> Path:
    cache = tmp_path / "cache" / "a.git"
    assert gitops.ensure_repo_cache(str(src), cache, local_source=local_source) is True
    assert _git("--git-dir", str(cache), "config", "--get", "remote.origin.url") == str(src)
    _commit(src, "two")
    return cache


def test_a_cache_gr2_seeded_from_a_local_checkout_refreshes(tmp_path: Path) -> None:
    src = _src_with_a_file(tmp_path)
    checkout = tmp_path / "checkout"
    _git("clone", "-q", str(src), str(checkout))
    cache = _seeded_from(src, checkout, tmp_path)
    _keys(cache)
    assert _refusal(src, cache) == ""
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == _git("-C", str(src), "rev-parse", "HEAD")


def test_a_cache_gr2_seeded_from_a_blobless_checkout_refreshes(tmp_path: Path) -> None:
    src = _src_with_a_file(tmp_path)
    _git("-C", str(src), "config", "uploadpack.allowFilter", "true")
    checkout = tmp_path / "checkout"
    _git("clone", "-q", "--filter=blob:none", f"file://{src}", str(checkout))
    assert _git("-C", str(checkout), "config", "--get", "remote.origin.promisor") == "true"
    cache = _seeded_from(src, checkout, tmp_path)
    keys = _keys(cache)
    assert not [key for key in keys if key.endswith(".promisor") or key.endswith(".partialclonefilter")], keys
    assert _refusal(src, cache) == ""
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == _git("-C", str(src), "rev-parse", "HEAD")


def test_a_cache_after_gc_and_maintenance_refreshes(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    _git("--git-dir", str(cache), "gc", "--auto")
    _git("--git-dir", str(cache), "maintenance", "run", "--auto")
    _keys(cache)
    assert _refusal(src, cache) == ""
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == _git("-C", str(src), "rev-parse", "HEAD")


# The user's own global config changes what `clone --mirror` writes, so these rows set their own global file.
def _user_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / "user-gitconfig"
    path.write_text(text)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))


def _refreshes(src: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "a.git"
    assert gitops.ensure_repo_cache(str(src), cache) is True
    _commit(src, "two")
    _keys(cache)
    assert _refusal(src, cache) == ""
    assert _git("--git-dir", str(cache), "rev-parse", "HEAD") == _git("-C", str(src), "rev-parse", "HEAD")


def test_a_cache_made_under_a_reftable_default_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    print(_git("--version"))
    probe = tmp_path / "probe"
    if subprocess.run(["git", "init", "-q", "--ref-format=reftable", str(probe)], capture_output=True).returncode:
        pytest.skip("this git cannot make a reftable repo")
    _user_global(tmp_path, monkeypatch, "[init]\n\tdefaultRefFormat = reftable\n")
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    _commit(src, "one")
    _refreshes(src, tmp_path)


def test_a_cache_made_under_a_user_template_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "config").write_text("[gc]\n\tauto = 0\n")
    _user_global(tmp_path, monkeypatch, f"[init]\n\ttemplateDir = {template}\n")
    src = tmp_path / "src"
    _git("init", "-q", str(src))
    _commit(src, "one")
    _refreshes(src, tmp_path)


def test_a_cache_of_a_sha256_source_refreshes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _user_global(tmp_path, monkeypatch, "")
    src = tmp_path / "src"
    _git("init", "-q", "--object-format=sha256", str(src))
    _commit(src, "one")
    _refreshes(src, tmp_path)


def test_a_refused_cache_names_its_path_and_how_to_reseed(tmp_path: Path) -> None:
    src, cache = _made_cache(tmp_path)
    _git("--git-dir", str(cache), "config", "gc.auto", "0")
    refusal = _refusal(src, cache)
    print(refusal)
    assert "config key gc.auto" in refusal, refusal
    assert f"removing {cache} makes gr2 seed a fresh one" in refusal, refusal

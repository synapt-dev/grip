"""`store materialize` installs the store's git hooks, so a root cloned with plain git gets the store's checks.

THE DEFECT, measured on grip dev 091d9142: the hooks were installed only by `store init`. A plain `git clone` copies
no hooks, so in a root cloned and then materialized, `.git/hooks` held nothing, and a plain `git add <member> && git
commit` recording a pin that is not on the member's remote exited 0. The same commit in the root `store init` made
was refused with "... push it first". `store status` there said to run `store init`, the wrong verb for a clone.

THE RULE: materialize runs the same installer `store init` runs. It never writes over another owner's hook or into a
`core.hooksPath`, and reports both on stderr; stdout is unchanged. A second run by the same gr2 leaves the hooks as
they are; a run by a different gr2 rewrites the store's own hooks to record that gr2's interpreter, while another
owner's hook is still never touched.

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gr2.python_cli import store_hooks
from tests.test_store_gitmodules_branch import _on_dev, _store
from tests.test_store_git_native_smoke import configure_identity, git, gr2, run


def _cloned(tmp_path: Path) -> Path:
    """A store made by `store init`, pushed, and cloned with plain git (no materialize yet)."""
    root = _store(tmp_path, _on_dev)
    remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "-q", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "-u", "origin", "HEAD:main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "-q", str(remote), str(fresh))
    configure_identity(fresh)
    return fresh


def _hook(root: Path, name: str) -> Path:
    return root / ".git" / "hooks" / name


def _commit_in(member: Path, name: str) -> str:
    (member / name).write_text(f"{name}\n")
    git(member, "add", name)
    git(member, "commit", "-q", "-m", name)
    return git(member, "rev-parse", "HEAD").stdout.strip()


def test_materialize_installs_both_hooks(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)
    assert not _hook(fresh, "pre-commit").exists(), "precondition: a plain clone carries no hooks"

    done = gr2(fresh, "materialize")

    for name in store_hooks.HOOK_NAMES:
        assert _hook(fresh, name).read_text() == store_hooks.hook_script(name), name
    assert "hooks:" not in done.stderr, done.stderr


def test_in_a_materialized_clone_plain_git_refuses_an_unpushed_pin(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)
    gr2(fresh, "materialize")
    beta = fresh / "beta"
    configure_identity(beta)
    unpushed = _commit_in(beta, "unpushed.txt")
    git(fresh, "add", "beta")

    refused = run(fresh, "git", "commit", "-q", "-m", "pin unpushed", check=False)

    assert refused.returncode != 0, refused.stderr
    assert f"beta pin {unpushed} is not on origin/dev; push it first" in refused.stderr, refused.stderr
    git(beta, "push", "-q", "origin", "HEAD:dev")   # control: the same pin, once pushed, commits
    assert run(fresh, "git", "commit", "-q", "-m", "pin pushed", check=False).returncode == 0
    assert git(fresh, "ls-tree", "HEAD", "beta").stdout.split()[2] == unpushed


def test_a_foreign_hook_in_the_clone_is_left_as_it_is_and_named(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)
    theirs = _hook(fresh, "pre-commit")
    theirs.parent.mkdir(parents=True, exist_ok=True)
    theirs.write_text("#!/bin/sh\n# someone else's\nexit 0\n")
    before = theirs.read_bytes()

    done = gr2(fresh, "materialize")

    assert theirs.read_bytes() == before
    assert "hooks: pre-commit belongs to another owner" in done.stderr, done.stderr
    assert _hook(fresh, "pre-push").read_text() == store_hooks.hook_script("pre-push")


def test_a_core_hooks_path_is_reported_and_nothing_is_written(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)
    elsewhere = tmp_path / "their-hooks"
    elsewhere.mkdir()
    git(fresh, "config", "core.hooksPath", str(elsewhere))

    done = gr2(fresh, "materialize")

    assert done.returncode == 0, done.stderr
    assert not any(_hook(fresh, name).exists() for name in store_hooks.HOOK_NAMES)
    assert list(elsewhere.iterdir()) == []
    assert "core.hooksPath is set" in done.stderr, done.stderr


def test_a_second_materialize_leaves_the_hooks_as_they_are(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)
    gr2(fresh, "materialize")
    first = {name: _hook(fresh, name).read_bytes() for name in store_hooks.HOOK_NAMES}

    again = gr2(fresh, "materialize")

    assert {name: _hook(fresh, name).read_bytes() for name in store_hooks.HOOK_NAMES} == first
    assert "hooks:" not in again.stderr, again.stderr


def test_status_in_a_clone_before_materialize_names_materialize(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)

    status = gr2(fresh, "status", check=False)

    said = status.stdout + status.stderr
    assert "hooks: pre-commit is not installed; run gr2 store materialize to install it" in said, said


def test_a_run_by_another_gr2_rewrites_the_stores_own_hooks_to_its_interpreter(tmp_path: Path, monkeypatch) -> None:
    """The installer compares the whole hook text, which records the interpreter, so another gr2 repoints the
    store's own hooks (the same rewrite that recovers a hook whose interpreter is gone); a foreign hook stays."""
    fresh = _cloned(tmp_path)
    theirs = _hook(fresh, "pre-commit")
    theirs.parent.mkdir(parents=True, exist_ok=True)
    theirs.write_text("#!/bin/sh\n# someone else's\nexit 0\n")
    gr2(fresh, "materialize")
    first = _hook(fresh, "pre-push").read_text()
    other = str(tmp_path / "other-venv" / "bin" / "python")
    monkeypatch.setattr(sys, "executable", other)

    states = store_hooks.install_store_hooks(fresh)

    assert states == {"pre-commit": "foreign", "pre-push": "installed"}, states
    assert _hook(fresh, "pre-push").read_text() == store_hooks.hook_script("pre-push") != first
    assert other in _hook(fresh, "pre-push").read_text()
    assert theirs.read_text() == "#!/bin/sh\n# someone else's\nexit 0\n"


def test_a_per_command_hooks_path_skips_the_hooks_the_documented_limit(tmp_path: Path) -> None:
    """A client hook is cooperative. Beside --no-verify, a core.hooksPath given for ONE command (`git -c`) skips it,
    and no configured-hooksPath report can see that; the store's own verbs are where the rule is enforced."""
    fresh = _cloned(tmp_path)
    gr2(fresh, "materialize")
    beta = fresh / "beta"
    configure_identity(beta)
    _commit_in(beta, "unpushed.txt")
    git(fresh, "add", "beta")
    assert run(fresh, "git", "commit", "-q", "-m", "x", check=False).returncode != 0, "control: the hook refuses"

    skipped = run(fresh, "git", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "x", check=False)

    assert skipped.returncode == 0, skipped.stderr


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="a 0o555 directory stops writes only for a non-root user on POSIX")
def test_a_hooks_dir_that_cannot_be_written_is_reported_and_materialize_still_succeeds(tmp_path: Path) -> None:
    fresh = _cloned(tmp_path)
    hooks = fresh / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hooks.chmod(0o555)
    try:
        done = gr2(fresh, "materialize", check=False)
    finally:
        hooks.chmod(0o755)

    assert done.returncode == 0, done.stderr
    assert "hooks: could not be installed" in done.stderr, done.stderr
    assert git(fresh / "beta", "rev-parse", "HEAD").returncode == 0   # the members landed

"""After a plain `git submodule update --remote <member>`, the member is still on its branch.

THE DEFECT, measured on grip dev fff9faf1 with #1230's registration in place: `git submodule update --remote beta`
moved beta to its branch's remote tip but left it DETACHED, with local `dev` at the old commit. With a local-only commit
on `dev`, the detached HEAD no longer contained that commit, so continuing work there quietly dropped it from the
checkout. That is the shape `store materialize` had before it learned to land on the branch.

THE RULE: the generated `.gitmodules` gives each member that declares a branch `update = merge`, git's own mode for
"advance the branch the member is on". `git submodule init` copies it into the root's config. With nothing local, the
merge is a fast-forward and the member's branch lands on the tip; with a local-only commit, the branch gains a merge
that keeps it. A member with no branch line gets no update line: there is no branch to stay on.

Fixtures are real git with bare local origins and no network; the `update --remote` rows name `protocol.file.allow`.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_store_gitmodules_branch import _on_dev, _push_one, _store
from tests.test_store_git_native_smoke import configure_identity, git, run


def _update_remote(root: Path):
    return run(root, "git", "-c", "protocol.file.allow=always", "submodule", "update", "--remote", "beta", check=False)


def _branch(member: Path) -> str:
    return git(member, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or "<detached>"


def _contains(member: Path, commit: str) -> bool:
    return git(member, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode == 0


def test_the_member_stays_on_its_branch_at_the_tip(tmp_path: Path) -> None:
    root = _store(tmp_path, _on_dev)
    tip = _push_one(tmp_path, "dev")

    moved = _update_remote(root)

    assert moved.returncode == 0, moved.stderr
    assert _branch(root / "beta") == "dev"
    assert git(root / "beta", "rev-parse", "HEAD").stdout.strip() == tip


def test_a_local_only_commit_is_kept_on_the_branch(tmp_path: Path) -> None:
    root = _store(tmp_path, _on_dev)
    member = root / "beta"
    (member / "local.txt").write_text("mine\n")
    git(member, "add", "local.txt")
    git(member, "commit", "-q", "-m", "local only")
    mine = git(member, "rev-parse", "HEAD").stdout.strip()
    tip = _push_one(tmp_path, "dev")

    moved = _update_remote(root)

    assert moved.returncode == 0, moved.stderr
    assert _branch(member) == "dev"
    assert _contains(member, mine) and _contains(member, tip)


def test_the_update_line_is_written_and_registered(tmp_path: Path) -> None:
    root = _store(tmp_path, _on_dev)
    assert git(root, "config", "-f", ".gitmodules", "--get", "submodule.beta.update").stdout.strip() == "merge"
    assert git(root, "config", "--get", "submodule.beta.update").stdout.strip() == "merge"


def test_a_member_with_no_branch_line_gets_no_update_line() -> None:
    from gr2.python_cli.grip_cli import render_gitmodules
    text = render_gitmodules([{"name": "beta", "path": "beta", "remote": "/r/beta.git", "upstream": ""}])
    assert "branch =" not in text and "update =" not in text, text


def test_control_without_the_update_mode_the_member_is_left_detached(tmp_path: Path) -> None:
    """Without this the rows above could pass because update --remote never detached in this fixture. Git falls back to
    `.gitmodules` for the update mode, so the control removes it there as well as from the root's config."""
    root = _store(tmp_path, _on_dev)
    git(root, "config", "--unset", "submodule.beta.update", check=False)
    git(root, "config", "-f", ".gitmodules", "--unset", "submodule.beta.update", check=False)
    tip = _push_one(tmp_path, "dev")

    _update_remote(root)

    assert _branch(root / "beta") == "<detached>"
    assert git(root / "beta", "rev-parse", "HEAD").stdout.strip() == tip


def test_registering_again_after_a_deinit_is_said_on_stderr(tmp_path: Path) -> None:
    """Git cannot tell a deinit from never-registered, so store commit registers again; it must say so, never silently
    undo the user's choice."""
    from tests.test_store_git_native_smoke import gr2

    root = _store(tmp_path, _on_dev)
    quiet = gr2(root, "commit", "-m", "nothing new")   # control: already registered, nothing to say
    assert "registered as a submodule" not in quiet.stderr, quiet.stderr
    git(root, "config", "--remove-section", "submodule.beta")   # what `git submodule deinit` leaves in config

    again = gr2(root, "commit", "-m", "nothing new")

    assert git(root, "config", "--get", "submodule.beta.url", check=False).returncode == 0
    assert "beta: registered as a submodule" in again.stderr, again.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the git stand-in is a POSIX shell script")
def test_a_new_registration_is_said_whatever_words_git_uses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A translated git words "registered for path" in its own language, so the new registration is read from the
    root's config, not from git's message. The stand-in rewrites that message the way a translation would."""
    from tests.test_store_git_native_smoke import gr2

    root = _store(tmp_path, _on_dev)
    git(root, "config", "--remove-section", "submodule.beta")
    real = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(
        "#!/bin/sh\n"
        f"exec 3>&1\n\"{real}\" \"$@\" 2>&1 1>&3 3>&- | sed 's/registered for path/für Pfad eingetragen/' 1>&2\n"
    )
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    probe = run(root, "git", "submodule", "init", "--", "beta")
    assert "für Pfad eingetragen" in probe.stderr, probe.stderr   # the stand-in is the git that ran
    git(root, "config", "--remove-section", "submodule.beta")

    again = gr2(root, "commit", "-m", "nothing new")

    assert "beta: registered as a submodule" in again.stderr, again.stderr


def test_only_the_member_registered_again_is_named(tmp_path: Path) -> None:
    """With the first member re-registered, a member after it that was registered all along says nothing: each
    member's line answers for that member alone."""
    from tests.test_store_git_native_smoke import gr2

    def two_members(member: Path) -> None:
        _on_dev(member)
        run(member.parent, "git", "clone", "-q", str(tmp_path / "beta.git"), "alpha")
        configure_identity(member.parent / "alpha")
        _on_dev(member.parent / "alpha")

    root = _store(tmp_path, two_members)
    for name in ("alpha", "beta"):   # precondition: both registered by the first commit
        assert git(root, "config", "--get", f"submodule.{name}.url", check=False).returncode == 0, name
    git(root, "config", "--remove-section", "submodule.alpha")

    again = gr2(root, "commit", "-m", "nothing new")

    assert "alpha: registered as a submodule" in again.stderr, again.stderr
    assert "beta: registered as a submodule" not in again.stderr, again.stderr

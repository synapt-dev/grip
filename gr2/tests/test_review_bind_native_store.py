"""`review bind` on a NATIVE store (the root `store init` makes), with no hand-made alpha store.

THE DEFECT, found by running `store init` then `review bind` on a fresh workspace: `store init`
writes `grip.toml` and a root `.git` and creates NO `.grip/.git`, but `review bind` writes the
review object into `.grip/.git` and refused with `not_initialized: No .grip/ directory ... Run
`gr2 store init .``, which is advice for the verb the caller had just run. Every existing bind
test hand-initialises the alpha store with `grip.grip_init`, which is why the native shape was
never exercised.

THE CONTRACT these rows pin:
  * on a native root, `review bind` creates the review store itself on first use (bind is the
    only writer);
  * a directory that is NOT a workspace keeps the refusal, and bind creates nothing in it;
  * a bind that refuses (base is not the live head) binds nothing;
  * a read verb on a native root with nothing bound says nothing has been bound, and does NOT
    send the caller back to `store init` (the verb they already ran).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from gr2.python_cli import grip as grip_mod

from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401


def _unpushed_head(ws: Path) -> tuple[str, str, str]:
    """One local commit in member `alpha`, never pushed. Returns (remote_url, base, head)."""
    alpha = ws / "alpha"
    remote = _git_out(alpha, "remote", "get-url", "origin")
    base = _git_out(alpha, "rev-parse", "origin/main")
    (alpha / "review-change.txt").write_text("under review\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "the change under review")
    return remote, base, _git_out(alpha, "rev-parse", "HEAD")


def _bind_args(ws: Path, remote: str, base: str, head: str) -> list[str]:
    return [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--source", str(ws / "alpha"),
    ]


def test_bind_on_a_fresh_native_store_needs_no_alpha_init(two_member_ws: Path) -> None:
    """On a fresh root, `store init` then `review bind` succeeds, and nothing in this test
    calls the engine's `grip_init`."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    assert not (ws / ".grip" / ".git").exists(), "precondition: store init makes no alpha store"
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    gr_id = out.strip().splitlines()[-1]
    assert gr_id.startswith("gr:") and len(gr_id) == 3 + 40, out

    assert (ws / ".grip" / ".git").is_dir()
    code, out = _cli("review", "verify", str(ws), gr_id)
    assert code == 0 and "tree_matches: True" in out, out


def test_bind_works_when_grip_exists_without_a_git_dir(two_member_ws: Path) -> None:
    """A native root can already hold `.grip/` (review records live under `.grip/state/`) with
    no `.git` inside it, the shape the old `.grip/ exists but has no .git/` refusal described."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    (ws / ".grip" / "state").mkdir(parents=True)
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert (ws / ".grip" / ".git").is_dir()


@pytest.mark.parametrize("shape", ["plain", "inside_another_repo", "git_repo_without_grip_toml"])
def test_bind_refuses_a_directory_that_is_no_native_root_and_creates_nothing(
    two_member_ws: Path, tmp_path: Path, shape: str
) -> None:
    """CONTROL: the new behaviour must not widen into 'bind initialises any directory'. Three
    shapes are not a native root: a plain directory; a directory INSIDE another git repo (git
    resolves that repo's info/exclude from there, so a bind that guessed would write to it); and
    a git repo with no `grip.toml`. Each keeps the refusal, gains no `.grip`, and leaves every
    repo's exclude file alone."""
    remote, base, head = _unpushed_head(two_member_ws)
    outer = tmp_path / "outer"
    outer.mkdir()
    target = outer
    if shape in ("inside_another_repo", "git_repo_without_grip_toml"):
        _git(outer, "init", "-q", "-b", "main")
    if shape == "inside_another_repo":
        target = outer / "inner"
        target.mkdir()

    code, out = _cli(*_bind_args(target, remote, base, head))
    assert code == 2 and "not_initialized" in out, out
    assert not (target / ".grip").exists(), f"bind created a store in a non-native root: {shape}"
    exclude = outer / ".git" / "info" / "exclude"
    assert not exclude.exists() or "/.grip/" not in exclude.read_text(), (
        f"bind wrote to another repo's exclude: {shape}"
    )


def test_a_refused_first_bind_leaves_no_review_store_behind(two_member_ws: Path) -> None:
    """A bind refused for `base_not_live_head` changes nothing: no commit, and no `.grip/.git`
    either, because the refused call is the one that would have created it."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)

    # base = head, which is not the live remote head
    code, out = _cli(*_bind_args(ws, remote, head, head))
    assert code == 2 and "base_not_live_head" in out, out
    assert not (ws / ".grip" / ".git").exists(), "a refused bind left an empty review store"
    assert not (ws / ".grip").exists(), "a refused bind left a .grip directory it created"


def test_a_leftover_after_a_refused_bind_is_named_and_the_refusal_still_surfaces(
    two_member_ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the store the refused call created cannot be fully removed, the caller is told what
    survived, and the bind's own refusal is not replaced by the cleanup failure. Removal that
    silently drops a failure (`ignore_errors=True`) reports a clean workspace that is not."""
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("a read-only directory only blocks removal for a non-root POSIX user")
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    store = ws / ".grip" / ".git"
    real = grip_mod._bind_review_rows

    def lock_then_bind(workspace, *args, **kwargs):
        os.chmod(workspace / ".grip" / ".git", 0o555)  # its entries can no longer be unlinked
        return real(workspace, *args, **kwargs)

    monkeypatch.setattr(grip_mod, "_bind_review_rows", lock_then_bind)
    try:
        code, out = _cli(*_bind_args(ws, remote, head, head))
    finally:
        if store.exists():
            os.chmod(store, 0o755)
    assert code == 2 and "base_not_live_head" in out, out
    assert "could not be fully removed" in out and str(store) in out, out


def test_a_refused_bind_keeps_a_grip_dir_that_was_already_there(two_member_ws: Path) -> None:
    """Only what the refused call created is removed: `.grip/state` was the owner's."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    kept = ws / ".grip" / "state"
    kept.mkdir(parents=True)
    (kept / "marker").write_text("keep\n")
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, head, head))
    assert code == 2 and "base_not_live_head" in out, out
    assert (kept / "marker").read_text() == "keep\n"
    assert not (ws / ".grip" / ".git").exists(), "a refused bind left an empty review store"


def test_a_refused_bind_keeps_an_empty_grip_dir_that_was_already_there(
    two_member_ws: Path,
) -> None:
    """`.grip` goes only when the refused call made it; an empty one the owner made stays."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    (ws / ".grip").mkdir()
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, head, head))
    assert code == 2, out
    assert (ws / ".grip").is_dir(), "the refused call removed a .grip directory it did not make"
    assert not (ws / ".grip" / ".git").exists()


def test_discarding_never_removes_a_store_that_holds_a_commit(two_member_ws: Path) -> None:
    """The guard in the cleanup: if a bind completed in the store (another caller's, say), a
    failed call's cleanup must not take it away. Driven directly, since two sequential binds
    never reach it."""
    from gr2.python_cli import grip

    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    gr_id = out.strip().splitlines()[-1]

    grip._discard_review_store(ws, True)
    assert (ws / ".grip" / ".git").is_dir(), "cleanup removed a store holding a bind"
    assert _cli("review", "verify", str(ws), gr_id)[0] == 0


def test_a_refused_bind_does_not_remove_a_store_that_already_holds_a_bind(
    two_member_ws: Path,
) -> None:
    """CONTROL for the cleanup: a store that existed before the refused call is never removed."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    first = out.strip().splitlines()[-1]

    code, out = _cli(*_bind_args(ws, remote, head, head))
    assert code == 2, out
    assert (ws / ".grip" / ".git").is_dir(), "the refused call removed an existing review store"
    assert _cli("review", "verify", str(ws), first)[0] == 0, "the earlier bind is gone"


def test_verify_on_a_native_root_with_nothing_bound_does_not_say_run_store_init(
    two_member_ws: Path,
) -> None:
    """The circular advice, on the verb that never writes: the caller already ran `store init`,
    so telling them to run it again is wrong. The message names what is actually missing."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0

    code, out = _cli("review", "verify", str(ws), "gr:" + "0" * 40)
    assert code == 2, out
    assert "store init" not in out and "grip init" not in out, out
    assert "bind" in out, out


# --- the root must not see the review store it just got ---------------------------------------
#
# An ADOPTED root (its own root .git and an owner .gitignore, which `store init` never edits)
# does not get the allow-list `.gitignore` that hides `.grip/`. Without an exclude, the first bind
# leaves `?? .grip/` in `git status`, and one `git add -A` records `.grip` as a 160000 gitlink to
# local-only review objects. The fix excludes it through the root's own `.git/info/exclude`, which
# is local to the clone and leaves the owner's `.gitignore` alone.


def _adopt(ws: Path) -> bytes:
    """Make `ws` the owner's own git repo, with an owner .gitignore, BEFORE `store init`."""
    _git(ws, "init", "-q", "-b", "main")
    owner = ws / ".gitignore"
    owner.write_text("alpha/\nbeta/\n*.log\n")
    _git(ws, "add", ".gitignore")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@e.invalid",
         "commit", "-q", "-m", "owner root")
    return owner.read_bytes()


def _status(ws: Path) -> str:
    return _git_out(ws, "status", "--porcelain")


def test_bind_on_an_adopted_root_leaves_the_review_store_out_of_git(two_member_ws: Path) -> None:
    ws = two_member_ws
    owner_bytes = _adopt(ws)
    assert _cli("store", "init", str(ws))[0] == 0
    assert ".grip" not in _status(ws), "precondition: nothing under .grip yet"
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert (ws / ".grip" / ".git").is_dir()

    assert ".grip" not in _status(ws), f"the root sees its review store:\n{_status(ws)}"
    _git(ws, "add", "-A")
    staged = _git_out(ws, "ls-files", "--stage")
    assert ".grip" not in staged, f"git add -A recorded the review store:\n{staged}"
    assert (ws / ".gitignore").read_bytes() == owner_bytes, "the owner's .gitignore was edited"

    # The root's own commit must not carry the review store either: the member is pushed so its
    # pin is covered, `store commit` makes the root commit, and that commit's tree has no .grip.
    _git(ws / "alpha", "push", "-q", "origin", "main")
    code, out = _cli("store", "commit", "-m", "after the bind")
    assert code == 0, out
    tree = _git_out(ws, "ls-tree", "-r", "--name-only", "HEAD")
    assert ".grip" not in tree, f"the root commit carries the review store:\n{tree}"
    assert "alpha" in tree.splitlines(), f"precondition: the member is in the tree:\n{tree}"


def test_the_exclude_line_is_not_duplicated_when_the_root_already_has_it(
    two_member_ws: Path,
) -> None:
    ws = two_member_ws
    _adopt(ws)
    assert _cli("store", "init", str(ws))[0] == 0
    exclude = ws / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# local\n/.grip/\n")
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert exclude.read_text().splitlines().count("/.grip/") == 1, exclude.read_text()

"""`review bind` on a NATIVE root (the one `store init` makes): the bind is a ref in the root's own `.git`.

THE HISTORY: `store init` writes `grip.toml` and a root `.git`; the first bind used to make a second
repo at `.grip/.git` and write the review object there. The bind now lives in the root's
`.git` under `refs/dev.synapt.grip/__reviews__/<commit>` (see test_review_bind_root_refs.py for the smallest
proof); this file pins the edges:
  * a bind needs no store and creates nothing under `.grip/`;
  * a directory that is NOT a workspace keeps the refusal, and bind creates nothing in it;
  * a bind that refuses (base is not the live head) binds nothing: no ref, no `.grip/.git`;
  * a read verb with nothing bound says nothing has been bound, and does NOT send the caller back
    to `store init`; an id that is not a bound review is refused as unbound;
  * `.grip/` is excluded by `store init` (an adopted root keeps its owner's `.gitignore`);
  * review binds an older gr2 left in `.grip/.git` move into refs automatically, once, verified.
"""

from __future__ import annotations

import os
import subprocess
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


_REFS = "refs/dev.synapt.grip/__reviews__/"


def _refs(ws: Path) -> list[str]:
    return [l for l in _git_out(ws, "for-each-ref", "--format=%(refname)", _REFS).splitlines() if l]


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

    assert not (ws / ".grip" / ".git").exists()
    assert _refs(ws) == [_REFS + gr_id[3:]]
    code, out = _cli("review", "verify", str(ws), gr_id)
    assert code == 0 and "tree_matches: True" in out, out


def test_bind_works_when_grip_exists_without_a_git_dir(two_member_ws: Path) -> None:
    """A native root can already hold `.grip/` (review records live under `.grip/state/`) with
    no `.git` inside it; a bind neither needs one nor adds one."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    (ws / ".grip" / "state").mkdir(parents=True)
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert not (ws / ".grip" / ".git").exists()
    assert len(_refs(ws)) == 1


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


def test_a_refused_first_bind_leaves_no_ref_and_no_store(two_member_ws: Path) -> None:
    """A bind refused for `base_not_live_head` changes nothing: no ref, no `.grip/.git`, and no
    `.grip` directory either."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, head, head))  # base = head, not the live remote head
    assert code == 2 and "base_not_live_head" in out, out
    assert _refs(ws) == []
    assert not (ws / ".grip" / ".git").exists()
    assert not (ws / ".grip").exists()


def test_a_refused_bind_keeps_what_was_already_in_grip(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    kept = ws / ".grip" / "state"
    kept.mkdir(parents=True)
    (kept / "marker").write_text("keep\n")
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, head, head))
    assert code == 2 and "base_not_live_head" in out, out
    assert (kept / "marker").read_text() == "keep\n"


def test_a_refused_bind_leaves_an_earlier_bind_intact(two_member_ws: Path) -> None:
    """CONTROL: a refused second bind adds no ref and does not disturb the first."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    first = out.strip().splitlines()[-1]

    code, out = _cli(*_bind_args(ws, remote, head, head))
    assert code == 2, out
    assert _refs(ws) == [_REFS + first[3:]]
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


def test_an_ordinary_root_commit_is_not_a_bound_review(two_member_ws: Path) -> None:
    """In the root's object database ANY commit resolves, so the bound-review ref is what makes
    an id a bind: the root's own HEAD commit is refused as unbound, not read as a malformed bind."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "root commit")[0] == 0
    root_head = _git_out(ws, "rev-parse", "HEAD")

    code, out = _cli("review", "verify", str(ws), "gr:" + root_head)
    assert code == 2 and "not_bound" in out, out


def test_an_abbreviated_id_still_resolves(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    gr_id = out.strip().splitlines()[-1]
    code, out = _cli("review", "verify", str(ws), gr_id[:3 + 12])
    assert code == 0 and "tree_matches: True" in out, out


# --- the root must not see gr2's state directory ------------------------------------------------
#
# An ADOPTED root (its own root .git and an owner .gitignore, which `store init` never edits)
# does not get the allow-list `.gitignore` that hides `.grip/`. `store init` therefore excludes it
# through the root's own `.git/info/exclude`, which is local to the clone and leaves the owner's
# `.gitignore` alone. (A bind writes nothing under `.grip/`; its record is a ref.)


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


def test_bind_on_an_adopted_root_leaves_grip_state_out_of_git(two_member_ws: Path) -> None:
    ws = two_member_ws
    owner_bytes = _adopt(ws)
    assert _cli("store", "init", str(ws))[0] == 0
    (ws / ".grip" / "state").mkdir(parents=True)
    (ws / ".grip" / "state" / "current_lane").write_text("x\n")
    assert ".grip" not in _status(ws), f"store init left .grip visible:\n{_status(ws)}"
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert not (ws / ".grip" / ".git").exists()

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
    exclude = ws / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# local\n/.grip/\n")
    assert _cli("store", "init", str(ws))[0] == 0
    assert exclude.read_text().splitlines().count("/.grip/") == 1, exclude.read_text()


# --- a root with no store gets one; real alpha state is refused, naming the conversion ----------------
#
# `store init` "needed before review" is the quirk this removes: a bind on a workspace root that has no
# store sets up the native store itself, says so on stderr, and a `.grip/.git` holding no bind and no
# snapshot counts as no store and is moved aside. Only a `.grip/.git` with real alpha state is refused,
# because `store migrate` can legitimately refuse (a pin not on the member's upstream).


def _spec_only_root(ws: Path) -> None:
    (ws / ".grip").mkdir()
    (ws / ".grip" / "workspace_spec.toml").write_text("")


def test_bind_on_a_workspace_root_with_no_store_sets_up_the_native_store(two_member_ws: Path, capfd) -> None:
    ws = two_member_ws
    _spec_only_root(ws)
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert "set up a native store" in out + capfd.readouterr().err
    assert (ws / "grip.toml").is_file() and (ws / ".git").exists()
    assert not (ws / ".grip" / ".git").exists()
    assert len(_refs(ws)) == 1
    gr_id = next(line for line in out.splitlines() if line.startswith("gr:"))
    assert _cli("review", "verify", str(ws), gr_id)[0] == 0


def test_bind_on_a_root_with_an_empty_alpha_store_moves_it_aside(two_member_ws: Path, capfd) -> None:
    from gr2.python_cli import grip as grip_mod

    ws = two_member_ws
    grip_mod.grip_init(ws)  # what workspace init and materialize used to leave: an empty `.grip/.git`
    remote, base, head = _unpushed_head(ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    assert "moved an empty .grip/.git aside" in out + capfd.readouterr().err
    assert not (ws / ".grip" / ".git").exists() and (ws / ".grip" / "legacy-store.git").is_dir()
    assert len(_refs(ws)) == 1


def test_bind_on_a_root_with_real_alpha_state_refuses_naming_store_migrate(two_member_ws: Path) -> None:
    from gr2.python_cli import grip as grip_mod

    ws = two_member_ws
    grip_mod.grip_init(ws)
    remote, base, head = _unpushed_head(ws)
    snapshot = grip_mod.create_workspace_commit(
        ws, [{"key": "alpha", "remote": remote, "path": "alpha", "commit": head, "base": base}])

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 2 and "alpha_root" in out and "store migrate" in out, out
    assert not (ws / "grip.toml").exists(), "a refused bind set up a native store"
    assert _git_out(ws / ".grip", "rev-parse", "HEAD") == snapshot, "a refused bind changed the alpha store"
    code, out = _cli("review", "verify", str(ws), "gr:" + "0" * 40)
    assert code == 2 and "store migrate" in out, out


def test_verify_on_a_root_with_no_store_creates_nothing(two_member_ws: Path) -> None:
    ws = two_member_ws
    _spec_only_root(ws)
    code, out = _cli("review", "verify", str(ws), "gr:" + "0" * 40)
    assert code == 2 and "not_bound" in out, out
    assert not (ws / "grip.toml").exists() and not (ws / ".git").exists()


def test_a_refusal_from_store_init_reaches_the_caller_unswallowed(tmp_path: Path, two_member_ws: Path) -> None:
    """The setup is `store init`'s, so its refusals are the bind's. A workspace root with no sibling git
    repository cannot be a store; the bind names that reason and writes nothing."""
    ws = tmp_path / "lonely"
    ws.mkdir()
    _spec_only_root(ws)
    remote, base, head = _unpushed_head(two_member_ws)

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 2 and "store_init_refused" in out and "no sibling git repositories" in out, out
    assert not (ws / "grip.toml").exists()

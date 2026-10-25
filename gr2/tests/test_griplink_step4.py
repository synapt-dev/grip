"""Full mode on the griplink root, step 4: promote, called as a function.

Promote is the one step that writes into a member's own git: the workspace change becomes one member commit, the pin
moves to it, and the member's STOOK goes. Carrying that deletion through push and pull is open beside 13.6 item 2.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import griplink
from gr2.python_cli.griplink import GriplinkRefusal

from tests.test_griplink_step1 import git, world  # noqa: F401  (api and web, pushed once)
from tests.test_griplink_step3 import _state, _write

R1_HEAD = f"api.griplink/{griplink.RECS}/r1/{griplink.HEADF}"


def _pin(root: Path, m: str = "api", rev: str = "HEAD") -> str:
    return git(root, "rev-parse", f"{rev}:{m}.griplink/{griplink.PIN}")


def _has_head(root: Path, m: str = "api", rev: str = "HEAD") -> bool:
    return bool(git(root, "ls-tree", "--name-only", rev, f"{m}.griplink/{griplink.HEADF}"))


def _change(ws: Path, msg: str = "api: two files, one gone") -> str:
    _write(ws, "api", "src/f1.py", "api change 1\n")
    _write(ws, "api", "src/new.py", "a new file\n")
    (griplink.member_dir(ws, "api") / "src" / "f2.py").unlink()
    git(griplink.member_dir(ws, "api"), "add", "-A")
    return griplink.commit(ws, msg)["shadow"]


def test_promote_writes_one_member_commit_moves_the_pin_and_drops_the_chain(world):
    ws = world["ws"]
    pin0, web0 = _pin(ws), _pin(ws, "web")
    _change(ws)
    tip = git(ws, "rev-parse", griplink.member_ref("api"))
    got = griplink.promote(ws, "api")
    mrepo = griplink.member_dir(ws, "api")
    new = git(mrepo, "rev-parse", "HEAD")
    assert got["commit"] == new and got["paths"] == 3
    assert git(mrepo, "rev-parse", f"{new}^@") == pin0  # one parent: the pin
    assert git(mrepo, "diff", "--name-status", pin0, new).splitlines() == [
        "M\tsrc/f1.py", "D\tsrc/f2.py", "A\tsrc/new.py"]
    assert git(mrepo, "log", "-1", "--format=%an <%ae> %s", new) == "x <x@x> api: two files, one gone"
    assert git(mrepo, "log", "-1", "--format=%ad", "--date=raw", new) == git(
        ws, "cat-file", "blob", f"{tip}:{griplink.GC_ADATE}")
    assert _pin(ws) == new and _pin(ws, "web") == web0
    assert not _has_head(ws)  # nothing left in the workspace for api
    assert git(ws, "cat-file", "blob", f"{griplink.STOOK}:api.griplink/{griplink.HEADF}") == \
        griplink.REFP + griplink.member_ref("api")
    assert git(ws, "for-each-ref", griplink.member_ref("api")) == ""
    assert git(mrepo, "status", "--porcelain") == ""
    assert griplink.status(ws) == {"moved": [], "unstaged": {}, "branch": "main", "upstream": "none"}


def test_promote_leaves_a_record_untouched(world):
    ws = world["ws"]
    _change(ws)
    rec = griplink.record(ws, "api", "r1", text="lgtm, one nit")
    griplink.commit(ws, "r1")
    held = git(ws, "rev-parse", f"HEAD:{R1_HEAD}")
    griplink.promote(ws, "api")
    assert git(ws, "rev-parse", griplink.record_ref("r1")) == rec
    assert git(ws, "rev-parse", f"HEAD:{R1_HEAD}") == held


def test_promote_of_a_pinned_head_makes_it_follow_again(world):
    ws = world["ws"]
    _change(ws)
    griplink.pin(ws, "api")
    griplink.commit(ws, "api pinned")
    griplink.promote(ws, "api")
    assert git(ws, "cat-file", "blob", f"{griplink.STOOK}:api.griplink/{griplink.HEADF}") == \
        griplink.REFP + griplink.member_ref("api")
    assert not _has_head(ws)


def test_nothing_to_promote_moves_nothing(world):
    ws = world["ws"]
    before = _state(ws)
    assert griplink.promote(ws, "api")["commit"] is None
    assert _state(ws) == before


def _unstaged(ws: Path) -> None:
    _change(ws)
    _write(ws, "api", "src/f3.py", "not staged\n")


def _moved_chain(ws: Path) -> None:
    _change(ws)
    tip = git(ws, "rev-parse", griplink.member_ref("api"))
    on = git(ws, "commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", "on the chain, not cut")
    git(ws, "update-ref", griplink.member_ref("api"), on)  # the chain is past what the shadow holds


def _cut_chain(ws: Path, tree: str, msg: str) -> None:
    """Put a hand-made GripCommit on api's chain and cut it, so the shadow holds what promote will read."""
    tip = griplink._cur(ws, griplink.member_ref("api"))
    c = git(ws, "commit-tree", tree, *(["-p", tip] if tip else []), "-m", msg)
    git(ws, "update-ref", griplink.member_ref("api"), c)
    griplink.commit(ws, msg)


def _bad_mode(ws: Path) -> None:
    pin = _pin(ws)
    field = griplink._field_tree(ws, {"src/sub": ("160000", "commit", pin)}, {})
    _cut_chain(ws, griplink._gripcommit_from_field(ws, pin, field, "a gitlink in the change"), "a gitlink in the change")


def _bad_author(ws: Path) -> None:
    _change(ws)
    tip = git(ws, "rev-parse", griplink.member_ref("api"))
    rows = [line.split("\t") for line in git(ws, "ls-tree", f"{tip}^{{tree}}").splitlines()]
    entries = []
    for meta, name in rows:
        mode, typ, sha = meta.split(" ")
        if name == griplink.GC_AUTHOR:
            sha = griplink._blob(ws, "x")
        entries.append((mode, typ, sha, name))
    _cut_chain(ws, griplink._mktree(ws, entries), "an author with no email")


def _no_pin(ws: Path) -> None:
    m = griplink.member_dir(ws, "api")
    shutil.rmtree(m)
    m.mkdir()
    git(m, "init", "-q")


def _mhead(ws: Path) -> str:
    return subprocess.run(["git", "-C", str(griplink.member_dir(ws, "api")), "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("setup,member,said", [
    (lambda ws: None, "nope", "nope: not a member of this workspace"),
    (lambda ws: None, "../api", "'../api': not a member name gr2 takes"),
    (_unstaged, "api", "api: its change is not all in a commit yet"),
    (_moved_chain, "api", "api: its change is not all in a commit yet"),
    (_bad_mode, "api", "api: the workspace change gives 'src/sub' mode 160000, which gr2 will not write"),
    (_no_pin, "api", "api: its checkout does not hold the pin"),
    (_bad_author, "api", "api: the change's author is not one gr2 writes into a commit"),
])
def test_a_promote_that_cannot_apply_refuses_at_3_before_any_ref_moves(world, setup, member, said):
    ws = world["ws"]
    setup(ws)
    before = _state(ws), _mhead(ws)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, member)
    assert e.value.code == 3 and str(e.value).startswith(said)
    assert (_state(ws), _mhead(ws)) == before


def test_a_member_not_checked_out_refuses(world):
    ws = world["ws"]
    _change(ws)
    s = world["tmp"] / "stranger"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{world['remote']}", str(s)], check=True,
                   capture_output=True)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(s, "api")
    assert e.value.code == 3 and "not checked out" in str(e.value)


def test_push_after_a_promote_pushes_and_leaves_the_remote_chain_alone(world):
    """Open beside 13.6 item 2: push and pull do not carry the deleted STOOK yet, so the remote keeps the old chain."""
    ws, remote = world["ws"], world["remote"]
    _change(ws)
    griplink.push(ws)
    old = git(remote, "rev-parse", griplink.member_ref("api"))
    new = griplink.promote(ws, "api")["commit"]
    got = griplink.push(ws)
    assert got["members"] == [] and git(remote, "rev-parse", "main") == got["shadow"]
    assert git(remote, "rev-parse", f"main:api.griplink/{griplink.PIN}") == new
    assert git(remote, "rev-parse", griplink.member_ref("api")) == old

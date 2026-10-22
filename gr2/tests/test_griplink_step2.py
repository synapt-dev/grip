"""Full mode on the griplink root, step 2: status, record, pin and follow, called as functions.

A record is its own chain, like commits: its head follows the record's branch in the STOOK and is followed to its sha
in the shadow. pin makes an entry's head a sha (it stops following); follow makes it a branch again. status says
whether the shadow is behind the STOOK, by entry, and names each member's unstaged tracked changes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import griplink
from gr2.python_cli.griplink import GriplinkRefusal

from tests.test_griplink_step1 import git, world  # noqa: F401  (the same workspace: api and web, pushed once)

R1_FIELD = f"api.griplink/{griplink.RECS}/r1/{griplink.HEADF}"


def _text(ws: Path, rev: str) -> str:
    """The record text a head field resolves to, read through its sha."""
    return git(ws, "cat-file", "blob", f"{git(ws, 'cat-file', 'blob', f'{rev}:{R1_FIELD}')}:{griplink.REC_TEXT}")


def test_status_reads_shadow_equals_stook_on_a_committed_workspace(world):
    assert griplink.status_lines(griplink.status(world["ws"])) == ["status: shadow = stook"]


def test_a_record_is_a_chain_followed_by_its_branch_and_resolved_in_the_shadow(world):
    ws = world["ws"]
    tip = griplink.record(ws, "api", "r1", text="lgtm")
    assert griplink.status_lines(griplink.status(ws)) == ["status: shadow BEHIND the stook: api/r1"]
    griplink.commit(ws, "r1")
    assert git(ws, "cat-file", "blob", f"{griplink.STOOK}:{R1_FIELD}") == griplink.REFP + griplink.record_ref("r1")
    assert git(ws, "cat-file", "blob", f"HEAD:{R1_FIELD}") == tip
    assert _text(ws, "HEAD") == "lgtm"
    assert git(ws, "cat-file", "blob", f"{tip}:{griplink.REC_COMMIT}") == git(ws, "rev-parse", "HEAD:api.griplink/001.2_pin")
    second = griplink.record(ws, "api", "r1", text="lgtm, one nit")
    assert git(ws, "rev-parse", f"{second}^") == tip, "a record appends to its chain"
    griplink.commit(ws, "r1 updated")
    assert _text(ws, "HEAD") == "lgtm, one nit"
    assert griplink.status_lines(griplink.status(ws)) == ["status: shadow = stook"]


def test_pin_holds_a_record_at_its_sha_and_follow_takes_the_branch_again(world):
    ws = world["ws"]
    griplink.record(ws, "api", "r1", text="lgtm")
    griplink.commit(ws, "r1")
    held = griplink.pin(ws, "api", "r1")
    griplink.commit(ws, "r1 pinned")
    assert git(ws, "cat-file", "blob", f"{griplink.STOOK}:{R1_FIELD}") == held
    griplink.record(ws, "api", "r1", text="a third version")
    assert griplink.status_lines(griplink.status(ws)) == ["status: shadow = stook"], "a pinned entry followed its branch"
    assert git(ws, "cat-file", "blob", f"HEAD:{R1_FIELD}") == held
    assert griplink.follow(ws, "api", "r1") == griplink.record_ref("r1")
    assert griplink.status_lines(griplink.status(ws)) == ["status: shadow BEHIND the stook: api/r1"]
    griplink.commit(ws, "r1 follows again")
    assert _text(ws, "HEAD") == "a third version"


def test_pin_holds_a_member_at_its_chains_sha_while_the_chain_moves_on(world):
    ws = world["ws"]
    (ws / "api" / "src" / "f1.py").write_text("api change 1\n")
    griplink.commit(ws, "one", stage_all=True)
    held = griplink.pin(ws, "api")
    griplink.commit(ws, "api pinned")
    (ws / "api" / "src" / "f2.py").write_text("api change 2\n")
    griplink.commit(ws, "two", stage_all=True)
    assert git(ws, "rev-parse", griplink.member_ref("api")) != held, "the chain did not move"
    assert git(ws, "cat-file", "blob", f"HEAD:api.griplink/{griplink.HEADF}") == held
    griplink.follow(ws, "api")
    assert griplink.status_lines(griplink.status(ws)) == ["status: shadow BEHIND the stook: api"]


def test_status_names_each_members_unstaged_tracked_changes_as_git_status_does(world):
    ws = world["ws"]
    (ws / "web" / "src" / "f2.py").write_text("web edit\n")
    assert griplink.status_lines(griplink.status(ws)) == [
        "status: shadow = stook",
        'web: changes not staged for commit: src/f2.py (use "git add" inside web, or "gr2 commit -a")']


@pytest.mark.parametrize("call, said", [
    (lambda ws: griplink.pin(ws, "api"), "api: its branch holds nothing yet"),
    (lambda ws: griplink.pin(ws, "api", "r9"), "api/r9: no such record under api"),
    (lambda ws: griplink.follow(ws, "nope"), "nope: not a member of this workspace"),
    (lambda ws: griplink.record(ws, "nope", "r1"), "nope: not a member of this workspace"),
    (lambda ws: griplink.record(ws, "api", "../r1"), "'../r1': not a member name gr2 takes"),
], ids=["pin-empty-branch", "pin-unknown-record", "follow-unknown-member", "record-unknown-member", "record-bad-id"])
def test_a_name_edit_that_cannot_apply_refuses_at_3_and_changes_nothing(world, call, said):
    ws = world["ws"]
    before = (git(ws, "rev-parse", "HEAD"), (ws / ".git" / "grip-pending.json").exists())
    with pytest.raises(GriplinkRefusal) as exc:
        call(ws)
    assert exc.value.code == 3 and str(exc.value).startswith(said), str(exc.value)
    assert (git(ws, "rev-parse", "HEAD"), (ws / ".git" / "grip-pending.json").exists()) == before


def test_one_record_id_belongs_to_one_member(world):
    # The chain lives at __records__/<id>, so web's r1 would be api's r1: the second member is refused.
    ws = world["ws"]
    tip = griplink.record(ws, "api", "r1", text="api says lgtm")
    pending = (ws / ".git" / "grip-pending.json").read_text()
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.record(ws, "web", "r1", text="web says block")
    assert exc.value.code == 3 and str(exc.value).startswith("r1: a record of api; one record id belongs to one member"), \
        str(exc.value)
    assert git(ws, "rev-parse", griplink.record_ref("r1")) == tip
    assert (ws / ".git" / "grip-pending.json").read_text() == pending
    griplink.commit(ws, "r1")
    assert _text(ws, "HEAD") == "api says lgtm"
    with pytest.raises(GriplinkRefusal):
        griplink.record(ws, "web", "r1", text="web says block")


def test_push_carries_the_record_chain_and_a_stranger_reads_it(world):
    ws, remote = world["ws"], world["remote"]
    tip = griplink.record(ws, "api", "r1", text="lgtm")
    griplink.commit(ws, "r1")
    got = griplink.push(ws)
    assert got["records"] == ["r1"] and got["members"] == [], got
    assert git(remote, "rev-parse", griplink.record_ref("r1")) == tip
    s = world["tmp"] / "stranger"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{remote}", str(s)], check=True, capture_output=True)
    git(s, "fetch", "-q", "origin", f"+{griplink.NS}/*:{griplink.NS}/*")
    assert _text(s, "HEAD") == "lgtm"
    assert git(s, "rev-parse", griplink.record_ref("r1")) == tip


def test_push_refuses_a_record_rewind_by_entry(world):
    ws, remote = world["ws"], world["remote"]
    griplink.record(ws, "api", "r1", text="lgtm")
    griplink.commit(ws, "r1")
    griplink.push(ws)
    pushed = git(ws, "rev-parse", griplink.record_ref("r1"))
    orphan = git(ws, "commit-tree", f"{pushed}^{{tree}}", "-m", "rewritten")
    git(ws, "update-ref", griplink.record_ref("r1"), orphan)
    griplink.commit(ws, "r1 rewritten")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 3 and str(exc.value).startswith(f"api/r1: {orphan[:12]} does not descend"), str(exc.value)
    assert git(remote, "rev-parse", griplink.record_ref("r1")) == pushed


def test_a_refused_push_hands_the_size_parser_member_refs_only(world, monkeypatch):
    ws, remote = world["ws"], world["remote"]
    (ws / "api" / "src" / "f1.py").write_text("api change 1\n")
    griplink.record(ws, "api", "r1", text="lgtm")
    griplink.commit(ws, "both", stage_all=True)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'refused by the test' >&2\nexit 1\n")
    hook.chmod(0o755)
    seen = []
    from gr2.python_cli import grip_cli
    monkeypatch.setattr(grip_cli, "host_size_refusal", lambda stderr, pushed: seen.append(pushed) or None)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 5
    assert [m for m, _, _ in seen[0]] == ["api"], seen

"""A bare `review pin` at a full root (`store init --mode full`, members added by `gr2 add`: no workspace spec and no
grip.toml) reads its members from the root commit's .gitmodules through the one set reader, and pins each from its
gitlink in HEAD. A workspace that declares no members refuses with words that say so, never "every member's checkout is
at the workspace's pin".

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * the full-root branch is dropped (members read as none)               -> the three-ahead, one-ahead, --members rows
  * the pin is read from the root's index, not HEAD                      -> the staged-gitlink row
  * the old default sentence returns for an empty member list            -> the no-members row
  * a reader refusal is read as "no members"                             -> the malformed-.gitmodules row
"""
from __future__ import annotations

import re
from pathlib import Path

from gr2.python_cli import declared_sets
from tests.test_full_root_declared_sets import MEMBERS, full  # noqa: F401
from tests.test_griplink_cli import git, gr2

OLD = "every member's checkout is at the workspace's pin"


def _flat(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def _out(result) -> str:
    return _flat((result.stdout or "") + (result.stderr or ""))


def _checkout(root: Path, member: str) -> Path:
    return root / declared_sets.read(root, "HEAD").members[member]["path"]


def _ahead(root: Path, member: str) -> str:
    checkout = _checkout(root, member)
    git(checkout, "checkout", "-q", "-b", "feat/f1")
    (checkout / f"{member}.txt").write_text(f"{member} f1\n")
    git(checkout, "commit", "-q", "-am", f"{member} f1")
    return git(checkout, "rev-parse", "HEAD")


def _bind_refused(r, out: str) -> None:
    """The rows are chosen and printed; binding them then stops at the store step, which does not read a full root yet
    (its own named refusal, a later change). Pinned as measured, so the day the bind lands this row says so."""
    assert r.exit_code == 2, out
    assert "does not read a full-mode root yet" in out, out


def _pin(root: Path, member: str) -> str:
    return git(root, "ls-tree", "HEAD", "--", declared_sets.read(root, "HEAD").members[member]["path"]).split()[2]


def test_full_root_every_member_ahead_is_pinned(full, monkeypatch):
    root = full["root"]
    pins = {m: _pin(root, m) for m in MEMBERS}
    heads = {m: _ahead(root, m) for m in MEMBERS}
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bind_refused(r, out)
    for m in MEMBERS:
        assert f"{m} {pins[m][:12]}..{heads[m][:12]}" in out or (pins[m] in out and heads[m] in out), out
    assert "nothing to pin" not in out


def test_full_root_one_member_ahead_names_the_other(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bind_refused(r, out)
    assert "web (checkout is at the workspace's pin)" in out, out


def test_full_root_members_flag_narrows(full, monkeypatch):
    root = full["root"]
    for m in MEMBERS:
        _ahead(root, m)
    r = gr2(root, monkeypatch, "review", "pin", "--members", "web")
    out = _out(r)
    _bind_refused(r, out)
    assert "api (excluded by --members)" in out, out


def test_full_root_staged_gitlink_is_not_the_base(full, monkeypatch):
    root = full["root"]
    pin = _pin(root, "api")
    head = _ahead(root, "api")
    git(root, "add", declared_sets.read(root, "HEAD").members["api"]["path"])  # staged, not committed
    assert git(root, "ls-files", "-s", "--", declared_sets.read(root, "HEAD").members["api"]["path"]).split()[1] == head
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bind_refused(r, out)
    assert pin[:12] in out and head[:12] in out, out


def test_full_root_unreadable_gitmodules_refuses_by_name(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    (root / ".gitmodules").write_text((root / ".gitmodules").read_text() + "[broken\n")
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "broken gitmodules")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == 2, out
    assert "gitmodules_unreadable" in out, out
    assert "nothing to pin" not in out


def test_no_members_refuses_with_its_own_words(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    git(plain, "init", "-q", "-b", "main")
    r = gr2(plain, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == 2, out
    assert "this workspace declares no members" in out, out
    assert OLD not in out, out

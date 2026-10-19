"""One member key rule at bind and at placement: grip.plain_member_key refuses a key a Windows or macOS
filesystem reads as another name, and grip.member_key_fold makes two spellings of one name one key."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

from gr2.python_cli import grip, open_gr_review
from tests.test_review_close_keeps_local_work import _placed_clone, _selection
from tests.review_ref_helper import legacy_review_ref
from tests.test_review_field_tree_readers import record_world  # noqa: F401
from tests.test_store_break_attempts import two_member_ws  # noqa: F401
from tests.test_review_transport import app, git, handoff, runner  # noqa: F401

_NFC = unicodedata.normalize("NFC", "d\u00e9mo-core")

# (key, member): each pair is one directory on APFS, HFS+ or NTFS.
ALIASES = [
    (unicodedata.normalize("NFD", _NFC), _NFC),  # NFD key beside an NFC member
    ("demo-core.", "demo-core"),  # NTFS drops a trailing dot
    ("demo-core ", "demo-core"),  # and a trailing space
    ("DEMO-C~1", "demo-core"),  # NTFS short name
    ("demo\u200c-core", "demo-core"),  # HFS+ ignores U+200C
    ("Demo-core", "demo-core"),  # case
    ("demo-core:x", "demo-core"),  # NTFS stream of demo-core
    ("\u200c.", "demo-core"),  # zero-width code points and dots
    (".\u200c.", "demo-core"),
    (".GIT", "demo-core"),  # the repository directory, any case
    (".Git", "demo-core"),
]


@pytest.mark.parametrize(("key", "member"), ALIASES, ids=[repr(k) for k, _ in ALIASES])
def test_an_alias_of_a_member_is_refused_at_bind_and_never_placed(handoff, tmp_path: Path, key: str, member: str):
    author, _, remote, _, base, head = handoff
    assert grip.plain_member_key(member)
    rows = [dict(key=k, path="member", remote=str(remote), base=base, head=head, ref="refs/heads/main",
                 title="alias", body="", source=str(author / "member")) for k in (member, key)]
    try:
        bind = f"bound {grip.create_review_bind_commit(author, rows)}"
    except grip.GripCorruptError as exc:
        bind = str(exc)
    lane = tmp_path / "lane"
    lane.mkdir()
    receipt = {"kind": "review-run", "downstream": _selection({key: _placed_clone(tmp_path, lane, key)})}
    placed = open_gr_review._run_placed_members(lane, receipt, {member})
    # Both sites are read before either is asserted, so a weaker rule shows red at each.
    assert ("invalid review key" in bind or "duplicate review key" in bind, placed) == (True, []), (bind, placed)


# Two spellings of one member a filesystem folds to one directory, as a bind written before the writer folded.
FOLDED = [("Demo-core", "demo-core"), (unicodedata.normalize("NFD", _NFC), _NFC)]


def _refs(root: Path) -> str:
    return git(root, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.mark.parametrize(("key", "member"), FOLDED, ids=[repr(k) for k, _ in FOLDED])
def test_a_bind_holding_two_spellings_of_one_member_reads_as_corrupt(
        handoff, tmp_path: Path, monkeypatch, key: str, member: str):
    author, _, remote, _, base, head = handoff
    rows = [dict(key=k, path=k, remote=str(remote), base=base, head=head, ref="refs/heads/main",
                 title="alias", body="", source=str(author / "member")) for k in (member, key)]
    with monkeypatch.context() as m:
        m.setattr(grip, "member_key_fold", lambda k: k)  # the writer before it folded
        commit = grip.create_review_bind_commit(author, rows)
    refs = _refs(author)
    # The bind sorts its members, so the spelling the reader meets second is the one it names.
    with pytest.raises(grip.GripCorruptError, match="member key (%s|%s)" % (re.escape(repr(key)), re.escape(repr(member)))):
        grip.show_review_commit(author, commit, migrate=False)
    lane = tmp_path / "lane"
    opened = runner.invoke(app, ["review", "open", str(author), f"gr:{commit}", "--lane-dir", str(lane)])
    assert (opened.exit_code != 0, lane.exists(), _refs(author)) == (True, False, refs), opened.output


def test_a_legacy_bind_holding_two_spellings_of_one_member_reads_as_corrupt(record_world):
    from tests.test_review_field_tree_readers import legacy_layout_bind
    w = record_world
    good = legacy_layout_bind(w)
    (name,) = grip.review_row_keys(w["author"], good)  # control
    alias = name.upper()

    def doubled(tree):
        rows = git(w["author"], "ls-tree", tree).splitlines()
        return git(w["author"], "mktree", input="".join(
            f"{line}\n" + (line.rsplit("\t", 1)[0] + "\t" + alias + "\n" if line.endswith("\t" + name) else "")
            for line in rows))

    root = git(w["author"], "rev-parse", good + "^{tree}")
    top = []
    for line in git(w["author"], "ls-tree", root).splitlines():
        meta, entry = line.split("\t")
        if entry in ("repos", "observed", "texts", "objects", "evidence"):
            meta = f"040000 tree {doubled(f'{root}:{entry}')}"
        top.append(f"{meta}\t{entry}\n")
    bad = git(w["author"], "commit-tree", git(w["author"], "mktree", input="".join(top)), "-m", "legacy two spellings")
    git(w["author"], "update-ref", legacy_review_ref(bad), bad, "0" * 40)
    # Git sorts the tree, so the spelling the reader meets second is the one it names.
    with pytest.raises(grip.GripCorruptError, match="member key (%s|%s)" % (re.escape(repr(alias)), re.escape(repr(name)))):
        grip.review_row_keys(w["author"], bad)

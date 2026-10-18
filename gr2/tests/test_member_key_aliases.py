"""One member key rule at bind and at placement: grip.plain_member_key refuses a key a Windows or macOS
filesystem reads as another name, and grip.member_key_fold makes two spellings of one name one key."""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from gr2.python_cli import grip, open_gr_review
from tests.test_review_close_keeps_local_work import _placed_clone, _selection
from tests.test_review_transport import handoff  # noqa: F401

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

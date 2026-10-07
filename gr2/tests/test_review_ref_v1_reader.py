"""The versioned review ref, reader side.

A writer of `the versioned review ref` must never meet a reader that breaks on it. Measured on grip
dev cecab8c8: with ANY `v1/` ref in the store, `list_review_binds` raised GripCorruptError ("noncanonical review
ref"), so every command that lists binds failed, and a bind present only as `v1/<id>` resolved as "not bound".
This is the reader-tolerant step: the review id is the LAST path segment, legacy and `v1` are the known
spellings, an unknown nested spelling is skipped with a warning, and nothing here writes a `v1/` ref.
"""

from __future__ import annotations


from pathlib import Path

import pytest

from tests.review_ref_helper import REVIEW_REF_ROOT
from gr2.python_cli import grip as grip_mod

from tests.test_review_bind_native_store import _bind_args, _unpushed_head
from tests.test_store_break_attempts import _cli, _commit_identity, _git, _git_out, two_member_ws  # noqa: F401

ROOT = REVIEW_REF_ROOT


def _bound(ws: Path) -> str:
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    return out.strip().splitlines()[-1][3:]


def _move_to_v1(ws: Path, commit: str) -> None:
    """What a release-B writer leaves: the same bind under `v1/` only. Written here by git, not by gr2."""
    _git(ws, "update-ref", f"{ROOT}v1/{commit}", commit)
    _git(ws, "update-ref", "-d", f"{ROOT}{commit}")


def _refs(ws: Path) -> list[str]:
    return [l for l in _git_out(ws, "for-each-ref", "--format=%(refname)", ROOT).splitlines() if l]


def test_a_legacy_and_a_v1_ref_to_one_bind_list_once(two_member_ws: Path) -> None:
    ws = two_member_ws
    commit = _bound(ws)
    _git(ws, "update-ref", f"{ROOT}v1/{commit}", commit)
    assert [c for c, _ in grip_mod.list_review_binds(ws)] == [commit]


def test_a_bind_present_only_under_v1_lists_resolves_shows_and_verifies(two_member_ws: Path) -> None:
    ws = two_member_ws
    commit = _bound(ws)
    _move_to_v1(ws, commit)
    assert _refs(ws) == [f"{ROOT}v1/{commit}"]
    assert [c for c, _ in grip_mod.list_review_binds(ws)] == [commit]
    assert grip_mod._resolve_bound(ws, commit) == commit
    code, out = _cli("review", "show", str(ws), "gr:" + commit)
    assert code == 0, out
    code, out = _cli("review", "verify", str(ws), "gr:" + commit)
    assert code == 0 and "tree_matches: True" in out, out


def test_an_unknown_nested_spelling_is_skipped_with_a_warning_not_fatal(two_member_ws: Path, capsys) -> None:
    ws = two_member_ws
    commit = _bound(ws)
    _git(ws, "update-ref", f"{ROOT}v9/{commit}", commit)
    assert [c for c, _ in grip_mod.list_review_binds(ws)] == [commit]
    assert f"{ROOT}v9/{commit}" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", ["not-a-sha", "v1/not-a-sha"])
def test_a_known_spelling_with_a_bad_id_is_still_corrupt(two_member_ws: Path, suffix: str) -> None:
    ws = two_member_ws
    commit = _bound(ws)
    _git(ws, "update-ref", f"{ROOT}{suffix}", commit)
    with pytest.raises(grip_mod.GripCorruptError, match="noncanonical review ref"):
        grip_mod.list_review_binds(ws)


def test_a_v1_ref_naming_another_commit_is_a_target_mismatch(two_member_ws: Path) -> None:
    ws = two_member_ws
    commit = _bound(ws)
    # A commit in the ROOT store that is not the bind (a native root has no HEAD, and a member's commits live
    # in the member's own store): a new commit over the bind's own tree.
    other = _git_out(ws, "commit-tree", f"{commit}^{{tree}}", "-m", "not the bind")
    assert other != commit
    _git(ws, "update-ref", "-d", f"{ROOT}{commit}")
    _git(ws, "update-ref", f"{ROOT}v1/{commit}", other)
    with pytest.raises(grip_mod.GripCorruptError, match="review_ref_target_mismatch"):
        grip_mod._resolve_bound(ws, commit)


@pytest.mark.parametrize("wrong", ["legacy", "v1"])
def test_one_valid_spelling_does_not_mask_another_naming_another_commit(two_member_ws: Path, wrong: str) -> None:
    """Both spellings present, one pointing elsewhere: resolve and the listing refuse, whichever is wrong."""
    ws = two_member_ws
    commit = _bound(ws)
    other = _git_out(ws, "commit-tree", f"{commit}^{{tree}}", "-m", "not the bind")
    legacy, v1 = f"{ROOT}{commit}", f"{ROOT}v1/{commit}"
    _git(ws, "update-ref", v1, other if wrong == "v1" else commit)
    _git(ws, "update-ref", legacy, other if wrong == "legacy" else commit)
    with pytest.raises(grip_mod.GripCorruptError, match="review_ref_target_mismatch"):
        grip_mod._resolve_bound(ws, commit)
    with pytest.raises(grip_mod.GripCorruptError, match="review_ref_target_mismatch"):
        grip_mod.list_review_binds(ws)


def test_two_valid_spellings_resolve(two_member_ws: Path) -> None:
    """Control: both spellings naming the bind resolve to it."""
    ws = two_member_ws
    commit = _bound(ws)
    _git(ws, "update-ref", f"{ROOT}v1/{commit}", commit)
    assert grip_mod._resolve_bound(ws, commit) == commit


def test_the_reader_step_writes_no_v1_ref(two_member_ws: Path) -> None:
    """A new bind is still ONE legacy ref; writing v1 refs is the later writer release."""
    ws = two_member_ws
    commit = _bound(ws)
    assert _refs(ws) == [f"{ROOT}{commit}"]


def test_transport_takes_known_spellings_and_refuses_an_unknown_version(two_member_ws: Path) -> None:
    """Receive and publish carry a bind under a spelling a reader knows (legacy or v1); a version no
    reader knows, or a ref naming another id, is refused."""
    full = "a" * 40
    for spelling in (f"{ROOT}{full}", f"{ROOT}v1/{full}"):
        assert grip_mod._review_transport_identity("gr:" + full, spelling) == (full, spelling)
    for bad in (f"{ROOT}v9/{full}", f"{ROOT}v1/{'b' * 40}"):
        with pytest.raises(grip_mod.GripCorruptError, match="review_ref_identity_mismatch"):
            grip_mod._review_transport_identity("gr:" + full, bad)

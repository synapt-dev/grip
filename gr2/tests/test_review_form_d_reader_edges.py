"""Edges of the form D readers that the reader red set does not pin: a record that fails
verify is refused by every reader, publish keeps the spelling a bind already has, and
receive refuses when another spelling of the same id names a different commit."""
from __future__ import annotations

import pytest

from gr2.python_cli import grip
from gr2.python_cli import review_form_d as fd
from tests.review_ref_helper import legacy_review_ref, review_ref
from tests.test_review_form_d_readers import attach, bound, record_world, ref_targets  # noqa: F401
from tests.test_review_transport import git
from tests.test_store_break_attempts import two_member_ws  # noqa: F401


def _duplicate_kind(root, tree):
    """The record with a second entry for field 2: a tree verify refuses."""
    blob = git(root, "hash-object", "-w", "--stdin", input="review")
    entries = git(root, "ls-tree", tree) + f"\n100644 blob {blob}\t002.2_kind_again\n"
    return git(root, "mktree", input=entries)


def test_readers_refuse_a_form_d_record_that_fails_verify(record_world, tmp_path):
    w = record_world
    assert grip.show_review_commit(w["author"], attach(w["author"], w["tree"]))["members"]  # control
    bad = _duplicate_kind(w["author"], w["tree"])
    commit = git(w["author"], "commit-tree", bad, "-m", "malformed")
    git(w["author"], "update-ref", review_ref(commit), commit, "0" * 40)
    for call, args in ((grip.show_review_commit, ()), (grip.verify_review_commit, ()),
                       (grip.review_row_keys, ()),
                       (grip.reconstruct_review_lane, ("recall", tmp_path / "lane"))):
        with pytest.raises(grip.GripCorruptError, match="invalid form D review record"):
            call(w["author"], commit, *args)
    assert not (tmp_path / "lane").exists()


def test_publish_sends_the_spelling_the_bind_has(bound):
    got = grip.publish_review_commit(bound["author"], bound["id"], bound["remote"])
    assert got["ref"] == review_ref(bound["id"])
    assert git(bound["remote"], "for-each-ref", "--format=%(refname)",
               "refs/dev.synapt.grip/__reviews__/").split() == [review_ref(bound["id"])]


def test_receive_refuses_when_another_spelling_names_a_different_commit(bound):
    git(bound["author"], "push", bound["remote"], f"{bound['id']}:{review_ref(bound['id'])}")
    other = git(bound["receiver"], "commit-tree", git(bound["receiver"], "mktree", input=""), "-m", "other")
    git(bound["receiver"], "update-ref", legacy_review_ref(bound["id"]), other, "0" * 40)
    with pytest.raises(grip.GripCorruptError, match="review_ref_target_mismatch"):
        grip.receive_review_commit(bound["receiver"], bound["id"], bound["remote"], ref=review_ref(bound["id"]))
    assert ref_targets(bound["receiver"]) == {legacy_review_ref(bound["id"]): other}


def _bound_variant(w, **member_changes):
    member = {**w["record"]["members"][0], **member_changes}
    member = {k: v for k, v in member.items() if v is not None}
    tree = fd.write_record(w["author"], {**w["record"], "members": [member]})
    commit = git(w["author"], "commit-tree", tree, "-m", "variant")
    git(w["author"], "update-ref", review_ref(commit), commit, "0" * 40)
    return commit


@pytest.mark.parametrize("key", ["../escape", "/tmp/pwn", "a/b", ".", "..", "a\\b", "a\nb", "a\0b"])
def test_a_member_key_that_is_not_a_plain_name_is_refused_before_any_path(record_world, tmp_path, key):
    w = record_world
    commit = _bound_variant(w, key=key)
    for call, args in ((grip.show_review_commit, ()), (grip.verify_review_commit, ()), (grip.review_row_keys, ()),
                       (grip.reconstruct_review_lane, (key, tmp_path / "lane"))):
        with pytest.raises(grip.GripCorruptError, match="member key"):
            call(w["author"], commit, *args)
    assert not (tmp_path / "lane").exists()


def test_a_plain_member_key_is_read(record_world):
    commit = _bound_variant(record_world, key="alpha")
    assert grip.review_row_keys(record_world["author"], commit) == ["alpha"]


@pytest.mark.parametrize("field", ["remote", "path", "commit", "base"])
def test_a_member_missing_a_repository_field_is_refused(record_world, field):
    commit = _bound_variant(record_world, **{field: None})
    with pytest.raises(grip.GripCorruptError, match=f"has no {field}"):
        grip.verify_review_commit(record_world["author"], commit)


@pytest.mark.parametrize("field", ["title", "body"])
def test_text_that_is_not_norm_is_refused(record_world, field):
    commit = _bound_variant(record_world, **{field: "text\n\n"})
    with pytest.raises(grip.GripCorruptError, match=f"{field} is not NORM text"):
        grip.verify_review_commit(record_world["author"], commit)

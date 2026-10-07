"""The v1 review ref names the field tree format: a record of the old layout never travels under it.

Without this check, a legacy-layout tree pushed under a remote v1 ref was accepted by
explicit and default receive, and a durable v1 ref was created. Controls: the same record under the
legacy spelling still receives, and a field tree under v1 still receives (test_review_ref_v1_writer).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import grip
from tests.native_root_helper import native_root
from tests.review_ref_helper import legacy_bind_tree, legacy_review_ref, review_ref
from tests.test_review_transport import git


def _legacy_record(tmp_path: Path) -> tuple[Path, Path, str]:
    source = native_root(tmp_path / "source")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    row = dict(key="alpha", path="alpha", remote=str(remote), base="a" * 40, head="b" * 40, title="", body="")
    commit = git(source, "commit-tree", legacy_bind_tree(source, row), "-m", "legacy record")
    return source, remote, commit


def _durable(root: Path) -> str:
    return git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/dev.synapt.grip")


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-ref", "default"])
def test_receive_refuses_a_legacy_layout_record_under_v1(tmp_path: Path, explicit: bool) -> None:
    source, remote, commit = _legacy_record(tmp_path)
    git(source, "push", "-q", str(remote), f"{commit}:{review_ref(commit)}")
    receiver = native_root(tmp_path / "receiver")
    with pytest.raises(grip.GripCorruptError, match="review_ref_format_mismatch"):
        grip.receive_review_commit(receiver, commit, str(remote), ref=review_ref(commit) if explicit else None)
    assert _durable(receiver) == ""


def test_receive_keeps_a_legacy_layout_record_under_the_legacy_spelling(tmp_path: Path) -> None:
    source, remote, commit = _legacy_record(tmp_path)
    git(source, "push", "-q", str(remote), f"{commit}:{legacy_review_ref(commit)}")
    receiver = native_root(tmp_path / "receiver")
    got = grip.receive_review_commit(receiver, commit, str(remote))
    assert got["ref"] == legacy_review_ref(commit)
    assert _durable(receiver) == f"{legacy_review_ref(commit)} {commit}"


def test_publish_refuses_a_legacy_layout_record_held_under_v1(tmp_path: Path) -> None:
    source, remote, commit = _legacy_record(tmp_path)
    git(source, "update-ref", review_ref(commit), commit)
    with pytest.raises(grip.GripCorruptError, match="review_ref_format_mismatch"):
        grip.publish_review_commit(source, commit, str(remote))
    assert git(remote, "for-each-ref") == ""

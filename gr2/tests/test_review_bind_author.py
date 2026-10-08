"""Binder identity is in the record, not inferred from member authors."""
# ruff: noqa: F811 -- imported pytest fixture is intentionally named in test arguments.
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import grip, review_field_tree as fd
from tests.review_ref_helper import review_ref
from tests.test_review_transport import git, handoff  # noqa: F401


@pytest.mark.parametrize("name", ["Binder One", "Binder Two"])
def test_bind_records_workspace_git_name_and_reads_it(handoff, name):
    root, _, remote, _, base, head = handoff
    git(root, "config", "user.name", name)
    git(root / "member", "config", "user.name", "Other member author")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               ref="refs/heads/main", source=str(root / "member"))
    commit = grip.create_review_bind_commit(root, [row])
    tree = git(root, "rev-parse", commit + "^{tree}")
    assert fd.read_record(root, tree).get("author") == name
    assert grip.show_review_commit(root, commit)["author"] == name
    assert grip.verify_review_commit(root, commit)["author"] == name


def test_old_field_tree_bind_stays_readable_without_inferred_author(handoff):
    root, _, _, commit, _, _ = handoff
    tree = git(root, "rev-parse", commit + "^{tree}")
    rows = [line for line in git(root, "ls-tree", tree).splitlines()
            if not line.endswith("\t005.2_author")]
    old_tree = git(root, "mktree", input="\n".join(rows) + "\n")
    old = git(root, "commit-tree", old_tree, "-m", "old bind")
    git(root, "update-ref", review_ref(old), old)
    git(root, "config", "user.name", "New reader identity")
    assert "author" not in fd.read_record(root, old_tree)
    assert "author" not in grip.show_review_commit(root, old)
    assert grip.verify_review_commit(root, old)["tree_matches"] is True
    assert "author" not in grip.verify_review_commit(root, old)
    assert git(root, "rev-parse", old + "^{tree}") == old_tree
    assert git(root, "rev-parse", review_ref(old)) == old


def test_missing_binder_name_refuses_before_any_publication(handoff, monkeypatch):
    root, _, remote, _, base, head = handoff
    # An explicitly empty local value overrides the otherwise valid fixture identity.
    git(root, "config", "user.name", "")
    before = git(root, "for-each-ref")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               ref="refs/heads/main", source=str(root / "member"))
    with pytest.raises(grip.GripReviewRefused, match="bind_author_unavailable.*git config user.name"):
        grip.create_review_bind_commit(root, [row])
    assert git(root, "for-each-ref") == before


def test_binder_identity_read_fault_refuses(handoff, monkeypatch):
    root, _, _, _, _, _ = handoff
    original = grip._bind_git

    def fault(workspace, *args):
        if args == ("config", "--get", "user.name"):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="broken config")
        return original(workspace, *args)

    monkeypatch.setattr(grip, "_bind_git", fault)
    with pytest.raises(grip.GripReviewRefused, match="bind_author_unavailable"):
        grip._review_bind_author(root)

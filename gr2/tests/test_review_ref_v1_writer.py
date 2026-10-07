"""Writer contracts: versioned refs and a form D tree, through real local Git stores.

These are deliberately ordinary assertions, not xfails. Legacy records used for
migration are built independently of whichever shape today's bind writer uses.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.review_ref_helper import REVIEW_REF_ROOT, review_ref, legacy_review_ref, legacy_bind_tree
from gr2.python_cli import grip
from gr2.python_cli import review_form_d as fd
from tests.test_review_transport import cli, git, handoff, root_owned_wrong_target  # noqa: F401


def refs(root: Path) -> dict[str, str]:
    return dict(line.split() for line in git(
        root, "for-each-ref", "--format=%(refname) %(objectname)", REVIEW_REF_ROOT
    ).splitlines())


def test_site6_publish_pushes_exactly_one_v1_ref(handoff, monkeypatch):
    author, _, remote, commit, _, _ = handoff
    result = cli(author, monkeypatch, "publish", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 0, result.output
    assert refs(remote) == {review_ref(commit): commit}, "site6: publish wrote a legacy or extra review ref"
    assert json.loads(result.stdout)["ref"] == review_ref(commit)


def test_site11_new_bind_creates_exactly_one_v1_ref(handoff):
    author, _, _, commit, _, _ = handoff
    assert refs(author) == {review_ref(commit): commit}, "site11: new bind wrote a legacy or extra review ref"


def test_site11_create_only_does_not_replace_an_existing_v1_target(handoff, monkeypatch):
    author, _, remote, _, base, head = handoff
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               ref="refs/heads/main", title="collision", body="", source=str(author / "member"))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_DATE", "2026-10-07T00:00:00Z")
    commit = grip.create_review_bind_commit(author, [row])
    wrong = root_owned_wrong_target(author, commit)
    git(author, "update-ref", review_ref(commit), wrong)
    before = refs(author)
    with pytest.raises((RuntimeError, grip.GripCorruptError), match="update-ref failed|review_ref_target_mismatch"):
        grip.create_review_bind_commit(author, [dict(row)])
    assert refs(author) == before, "site11: create-only collision changed an existing ref"
    assert git(author, "rev-parse", review_ref(commit)) == wrong


def test_same_record_and_commit_inputs_give_the_same_id(handoff, monkeypatch):
    author, _, remote, _, base, head = handoff
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               ref="refs/heads/main", title="fixed title", body="fixed body", source=str(author / "member"))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "fixture@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2026-10-07T00:00:00Z")
    first = grip.create_review_bind_commit(author, [row])
    first_tree = git(author, "rev-parse", first + "^{tree}")
    second = grip.create_review_bind_commit(author, [dict(row)])
    assert git(author, "rev-parse", second + "^{tree}") == first_tree
    assert first == second, "section7: identical record and commit inputs changed the review id"
    assert git(author, "rev-list", "--parents", "-n", "1", first).split() == [first]


def test_a_bind_record_is_a_form_d_tree_accepted_as_written(handoff):
    author, _, remote, commit, base, head = handoff
    tree = git(author, "rev-parse", commit + "^{tree}")
    try:
        fd.verify_tree(author, tree)
    except fd.ReviewRecordError as exc:
        pytest.fail(f"section7: bind tree is not a valid form D record: {exc}")
    record = fd.read_record(author, tree)
    assert record["kind"] == "review"
    assert [(m["key"], m["path"], m["remote"], m["base"], m["commit"])
            for m in record["members"]] == [("member", "member", str(remote), base, head)]
    assert record["members"][0]["range_patch"], "form D record lost the reviewed range"
    assert git(author, "rev-parse", commit + "^{tree}") == tree, "verification rewrote the record"


@pytest.mark.parametrize("payload,reason", [
    (b"\x12\x06review\x12\x06review", "wire type 2"),
    (b"\x10\x01", "wire type 2"),
], ids=["duplicate-singular-kind", "kind-as-varint"])
def test_writer_verifies_invalid_bytes_before_commit_or_ref(handoff, monkeypatch, tmp_path, payload, reason):
    from tests.native_root_helper import native_root
    author, _, remote, _, base, head = handoff
    root = native_root(tmp_path / "fresh-writer")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               ref="refs/heads/main", title="", body="", source=str(author / "member"))
    calls = []
    original = fd.encode
    def invalid(record, message="ReviewBind"):
        if message == "ReviewBind":
            calls.append(message)
            return payload
        return original(record, message)
    # Prove this corrupt byte seam CAN create a tree, and that verify rejects
    # that actual tree; encode's normal input validation cannot stand in for it.
    bad_tree = fd.write_tree(root, payload)
    with pytest.raises(fd.ReviewRecordError) as exc:
        fd.verify_tree(root, bad_tree)
    assert reason in str(exc.value)
    before = refs(root)
    before_commits = {line.split()[0] for line in git(
        root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    ).splitlines() if line.endswith(" commit")}
    monkeypatch.setattr(fd, "encode", invalid)
    failure = None
    try:
        grip.create_review_bind_commit(root, [row])
    except fd.ReviewRecordError as exc:
        failure = str(exc)
    assert refs(root) == before == {}, "verify-after-write: invalid bytes reached a durable ref"
    after_commits = {line.split()[0] for line in git(
        root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    ).splitlines() if line.endswith(" commit")}
    assert after_commits == before_commits, "verify-after-write: invalid bytes reached commit-tree"
    assert calls == ["ReviewBind"], "production writer did not traverse the record-to-bytes seam"
    assert failure is not None and reason in failure
    # A valid control through the SAME production writer must bind successfully.
    monkeypatch.setattr(fd, "encode", original)
    commit = grip.create_review_bind_commit(root, [row])
    fd.verify_tree(root, git(root, "rev-parse", commit + "^{tree}"))
    assert refs(root) == {review_ref(commit): commit}


def test_project_review_caller_keeps_legacy_destination_and_reconstruction(handoff, tmp_path):
    author, _, remote, _, base, head = handoff
    from tests.native_root_helper import native_root
    root = native_root(tmp_path / "project-root")
    pin = dict(key="member", path="member", repo=str(remote), base=base, head=head)
    patch = git(author / "member", "format-patch", "--stdout", base + ".." + head)
    committers = git(author / "member", "log", "--reverse", "--format=%cn%x09%ce%x09%cI", base + ".." + head) + "\n"
    commit = grip.create_project_review_commit(root, [pin], {"member": patch}, {"member": committers})
    assert refs(root) == {legacy_review_ref(commit): commit}, "project caller changed its format destination to v1"
    assert git(root, "show", commit + ":.grip/schema") == grip._PROJECT_REVIEW_SCHEMA
    assert grip.read_project_review_commit(root, commit) == [pin]
    lane = tmp_path / "project-lane"
    got = grip.reconstruct_project_review_lane(root, commit, "member", lane)
    assert got["reconstructed_head"] == head
    assert git(lane, "rev-parse", "HEAD^{tree}") == git(author / "member", "rev-parse", "HEAD^{tree}")
    assert (lane / "payload.txt").read_text() == "reviewed\n"


@pytest.mark.parametrize("version", ["", "v1"], ids=["legacy-source", "v1-source"])
def test_receive_keeps_requested_source_and_destination_spelling(handoff, monkeypatch, tmp_path, version):
    from tests.native_root_helper import native_root
    _, _, remote, _, base, head = handoff
    source = native_root(tmp_path / "transport-source")
    receiver = native_root(tmp_path / "transport-receiver")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               title="legacy title", body="legacy body")
    if version:
        record = {"schema": grip._REVIEW_BIND_SCHEMA, "kind": "review", "policy": "no-policy",
                  "members": [{"key": "member", "path": "member", "remote": str(remote),
                               "base": base, "commit": head, "remote_head": base,
                               "title": row["title"], "body": row["body"]}]}
        tree = fd.write_record(source, record)
        fd.verify_tree(source, tree)
    else:
        tree = legacy_bind_tree(source, row)
    commit = git(source, "commit-tree", tree, "-m", "original received review")
    requested = review_ref(commit, version=version)
    git(source, "update-ref", requested, commit, "0" * 40)
    git(source, "push", remote, f"{commit}:{requested}")
    assert refs(remote) == {requested: commit}
    before = git(source, "cat-file", "-p", commit)
    for _ in range(2):
        result = cli(receiver, monkeypatch, "receive", "gr:" + commit,
                     "--remote", remote, "--ref", requested)
        assert result.exit_code == 0, result.output
        got = json.loads(result.stdout)
        assert got["ref"] == requested
        assert refs(receiver) == {requested: commit}, "receive converted or relabelled the requested format"
        assert git(receiver, "cat-file", "-p", commit) == before
        assert git(receiver, "rev-parse", commit + "^{tree}") == tree

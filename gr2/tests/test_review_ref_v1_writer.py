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


def put_remote_v1(handoff, *, legacy: bool = False) -> None:
    author, _, remote, commit, _, _ = handoff
    git(author, "push", remote, f"{commit}:{review_ref(commit)}")
    if legacy:
        git(author, "push", remote, f"{commit}:{legacy_review_ref(commit)}")
    assert refs(remote) == {
        **({legacy_review_ref(commit): commit} if legacy else {}),
        review_ref(commit): commit,
    }, "transport fixture must expose the intended exact ref spelling"


def test_site6_publish_pushes_exactly_one_v1_ref(handoff, monkeypatch):
    author, _, remote, commit, _, _ = handoff
    result = cli(author, monkeypatch, "publish", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 0, result.output
    assert refs(remote) == {review_ref(commit): commit}, "site6: publish wrote a legacy or extra review ref"
    assert json.loads(result.stdout)["ref"] == review_ref(commit)


def test_site11_new_bind_creates_exactly_one_v1_ref(handoff):
    author, _, _, commit, _, _ = handoff
    assert refs(author) == {review_ref(commit): commit}, "site11: new bind wrote a legacy or extra review ref"


def test_site11_create_only_does_not_replace_an_existing_v1_target(handoff):
    author, _, _, commit, _, _ = handoff
    wrong = root_owned_wrong_target(author, commit)
    git(author, "update-ref", review_ref(commit), wrong)
    before = refs(author)
    with pytest.raises(RuntimeError, match="update-ref failed"):
        grip._publish_bind(author, commit, "same reviewed bind")
    assert refs(author) == before, "site11: create-only collision changed an existing ref"
    assert git(author, "rev-parse", review_ref(commit)) == wrong


def test_site5_receive_accepts_an_explicit_v1_publish(handoff, monkeypatch):
    _, receiver, remote, commit, _, _ = handoff
    put_remote_v1(handoff)
    result = cli(receiver, monkeypatch, "receive", "gr:" + commit,
                 "--remote", remote, "--ref", review_ref(commit))
    assert result.exit_code == 0, f"site5: explicit v1 transport was refused: {result.output}"
    assert refs(receiver) == {review_ref(commit): commit}


def test_site8_receive_default_fetches_the_v1_only_publish(handoff, monkeypatch):
    _, receiver, remote, commit, _, _ = handoff
    put_remote_v1(handoff)
    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 0, f"site8: receive looked for the wrong published ref: {result.output}"
    assert refs(receiver) == {review_ref(commit): commit}
    assert json.loads(result.stdout)["ref"] == review_ref(commit)
    assert git(receiver, "for-each-ref", "refs/dev.synapt.grip/__review_transfers__/") == ""


def test_site9_existing_v1_bind_is_received_without_adding_a_legacy_ref(handoff, monkeypatch):
    author, receiver, remote, commit, _, _ = handoff
    # Both remote spellings let a legacy fetch succeed, exposing the local
    # existing-bind mistake independently of the earlier fetch lookup failure.
    put_remote_v1(handoff, legacy=True)
    git(receiver, "fetch", "--no-tags", author, f"{commit}:{review_ref(commit)}")
    before = refs(receiver)
    for _ in range(2):
        result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
        assert result.exit_code == 0, result.output
        assert refs(receiver) == before, "site9: receive added a legacy ref beside the existing v1 bind"


def test_site9_existing_v1_ref_with_wrong_target_is_refused(handoff, monkeypatch):
    author, receiver, remote, commit, _, _ = handoff
    put_remote_v1(handoff, legacy=True)
    git(receiver, "fetch", "--no-tags", author, commit)
    wrong = root_owned_wrong_target(receiver, commit)
    git(receiver, "update-ref", review_ref(commit), wrong)
    before = refs(receiver)
    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 2 and "review_ref_target_mismatch" in result.output, (
        f"site9: receive ignored the conflicting v1 bind: {result.output}"
    )
    assert refs(receiver) == before


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


def test_site10_legacy_store_migrates_to_v1_without_rewriting_record(handoff):
    author, receiver, remote, _, base, head = handoff
    legacy = receiver / ".grip"
    legacy.mkdir(exist_ok=True)
    git(legacy, "init", "-q", "-b", "main")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               title="legacy title", body="legacy body")
    tree = legacy_bind_tree(legacy, row)
    commit = git(legacy, "commit-tree", tree, "-m", "legacy review bind")
    git(legacy, "update-ref", "HEAD", commit)
    before = git(legacy, "cat-file", "-p", commit)
    assert git(legacy, "show", commit + ":.grip/schema") == "gr2-review-bind/v2"
    grip._migrate_legacy_binds(receiver)
    assert refs(receiver) == {review_ref(commit): commit}, "site10: migration published a legacy or extra ref"
    assert git(receiver, "cat-file", "-p", commit) == before, "migration changed commit inputs/id"
    assert git(receiver, "rev-parse", commit + "^{tree}") == tree, "migration rewrote legacy record tree"
    assert grip.verify_review_commit(receiver, commit)["tree_matches"] is True
    aside = receiver / ".grip" / "legacy-store.git"
    assert aside.is_dir() and not (legacy / ".git").exists()
    assert git(aside, "rev-parse", "HEAD") == commit
    grip._migrate_legacy_binds(receiver)
    assert refs(receiver) == {review_ref(commit): commit}

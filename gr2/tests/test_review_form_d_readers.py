"""Reader-first form D witnesses, independent of the production bind writer.

Each form D fixture is written by the adapter, committed and attached by Git
under a v1 ref. Legacy controls use the unchanged production writer. There is
no new-writer or migration contract in this file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gr2.python_cli import grip
from gr2.python_cli import review_form_d as fd
from tests.native_root_helper import native_root
from tests.review_ref_helper import REVIEW_REF_ROOT, legacy_review_ref, review_ref
from tests.test_review_bind_verify import BODY, TITLE, _fixture_remote, _row
from tests.test_review_form_d import _rename_entry
from tests.test_review_transport import git
from tests.test_store_break_attempts import two_member_ws  # noqa: F401

EVIDENCE = b"label: reviewed-content\ncommand: git show HEAD:f.txt\nexit: 0\n"


def ref_targets(root: Path) -> dict[str, str]:
    return dict(line.split() for line in git(
        root, "for-each-ref", "--format=%(refname) %(objectname)", REVIEW_REF_ROOT
    ).splitlines())


def read(call, *args, **kwargs):
    """Surface the domain refusal as a named reader failure, not an opaque rc."""
    try:
        return call(*args, **kwargs)
    except (grip.GripCorruptError, grip.GripReviewRefused) as exc:
        pytest.fail(f"reader {call.__name__} refused a valid form D record: {exc}")


def attach(root: Path, tree: str) -> str:
    fd.verify_tree(root, tree)
    commit = git(root, "commit-tree", tree, "-m", "test-owned form D review")
    git(root, "update-ref", review_ref(commit), commit, "0" * 40)
    assert ref_targets(root) == {review_ref(commit): commit}
    assert git(root, "rev-parse", commit + "^{tree}") == tree
    return commit


@pytest.fixture
def record_world(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"
    author = native_root(tmp_path / "author")
    receiver = native_root(tmp_path / "receiver")
    patch = git(work, "format-patch", "--stdout", base + ".." + head).encode()
    metadata = git(work, "log", "--format=fuller", base + ".." + head).encode()
    committers = (git(work, "log", "--reverse", "--format=%cn%x09%ce%x09%cI", base + ".." + head) + "\n").encode()
    member = dict(key="recall", path="recall", remote=remote, base=base, commit=head,
                  head_tree=git(work, "rev-parse", "HEAD^{tree}"), metadata=metadata,
                  range_patch=patch, committers=committers, remote_head=base,
                  title=TITLE.rstrip("\n"), body=BODY.rstrip("\n"),
                  evidence={"commands": EVIDENCE, "resolution": b"fixture resolution"})
    record = dict(schema=grip._REVIEW_BIND_SCHEMA, kind="review", policy="no-policy", members=[member])
    tree = fd.write_record(author, record)
    assert fd.read_record(author, tree) == record, "test fixture must represent every carried field"
    return dict(author=author, receiver=receiver, remote=remote, base=base, head=head,
                work=work, tree=tree, record=record)


@pytest.fixture
def bound(record_world):
    w = record_world
    w["id"] = attach(w["author"], w["tree"])
    return w


def test_form_d_fixture_is_real_and_verifies_as_written(bound):
    """Control: failures below cannot be caused by an invalid test-owned tree."""
    fd.verify_tree(bound["author"], bound["tree"])
    assert fd.read_record(bound["author"], bound["tree"]) == bound["record"]
    assert git(bound["work"], "rev-parse", "origin/dev") == bound["base"]
    assert git(bound["remote"], "rev-parse", "dev") == bound["base"]


def test_show_reads_real_form_d_member_values_and_carried_files(bound):
    got = read(grip.show_review_commit, bound["author"], bound["id"])
    assert got == {"id": "gr:" + bound["id"], "members": [{
        "key": "recall", "remote": bound["remote"], "path": "recall",
        "base": bound["base"], "head": bound["head"],
        "title": TITLE.rstrip("\n"), "body": BODY.rstrip("\n"), "files": ["f.txt"],
    }]}


def test_verify_checks_real_form_d_tree_and_norm_text_hashes(bound):
    before = git(bound["author"], "cat-file", "-p", bound["id"])
    got = read(grip.verify_review_commit, bound["author"], bound["id"])
    assert got["tree_matches"] is True
    assert got["stored_tree"] == bound["tree"]
    row = got["rows"][0]
    assert (row["key"], row["base"], row["head"], row["observed_remote_head"]) == (
        "recall", bound["base"], bound["head"], bound["base"])
    for key, value in (("title", TITLE), ("body", BODY)):
        assert row[key + "_sha256"] == hashlib.sha256(value.rstrip("\n").encode()).hexdigest()
    assert git(bound["author"], "cat-file", "-p", bound["id"]) == before
    assert ref_targets(bound["author"]) == {review_ref(bound["id"]): bound["id"]}


def test_reconstruct_reads_form_d_range_and_committers_exactly(bound, tmp_path):
    lane = tmp_path / "lane"
    got = read(grip.reconstruct_review_lane, bound["author"], bound["id"], "recall", lane)
    assert got["reconstructed_head"] == bound["head"]
    assert got["reconstructed_tree"] == bound["record"]["members"][0]["head_tree"]
    assert (lane / "f.txt").read_text() == "head\n"
    assert git(bound["remote"], "rev-parse", "dev") == bound["base"]


def test_run_checks_reads_form_d_evidence_and_runs_inside_reconstruction(bound, tmp_path):
    lane = tmp_path / "checked-lane"
    got = read(grip.run_review_checks, bound["author"], bound["id"], "recall", lane)
    assert got["materialized"]["reconstructed_head"] == bound["head"]
    assert len(got["runs"]) == 1
    row = got["runs"][0]
    assert (row["label"], row["cwd"], row["exit"], row["exit_matched"]) == (
        "reviewed-content", str(lane), 0, True)
    assert row["output_digest"] == hashlib.sha256(b"head\n").hexdigest()
    assert got["import_resolution"] == str(lane)


def test_review_row_keys_reads_the_form_d_members(bound):
    assert read(grip.review_row_keys, bound["author"], bound["id"]) == ["recall"]


def test_publish_content_gate_accepts_valid_form_d_without_rewriting(bound):
    got = read(grip.publish_review_commit, bound["author"], bound["id"], bound["remote"])
    assert got["id"] == "gr:" + bound["id"]
    assert git(bound["remote"], "rev-parse", got["ref"]) == bound["id"]
    assert git(bound["remote"], "rev-parse", bound["id"] + "^{tree}") == bound["tree"]


def test_receive_accepts_v1_form_d_and_returned_ref_is_the_durable_ref(bound):
    git(bound["author"], "push", bound["remote"], f"{bound['id']}:{review_ref(bound['id'])}")
    assert ref_targets(Path(bound["remote"])) == {review_ref(bound["id"]): bound["id"]}
    for _ in range(2):
        got = read(grip.receive_review_commit, bound["receiver"], bound["id"], bound["remote"],
                   ref=review_ref(bound["id"]))
        assert got["id"] == "gr:" + bound["id"]
        assert ref_targets(bound["receiver"]) == {got["ref"]: bound["id"]}
        assert git(bound["receiver"], "rev-parse", bound["id"] + "^{tree}") == bound["tree"]
        assert read(grip.verify_review_commit, bound["receiver"], bound["id"])["tree_matches"] is True
        assert git(bound["receiver"], "for-each-ref", "refs/dev.synapt.grip/__review_transfers__/") == ""


def altered_tree(w, variant):
    tree = w["tree"]
    if variant == "noncurrent-name":
        return _rename_entry(w["author"], tree, "004.2r_members", "0000001", "010.2_title", "010.2_old_title_name")
    blob = git(w["author"], "hash-object", "-w", "--stdin", input="a future field\n")
    entries = git(w["author"], "ls-tree", tree) + f"\n100644 blob {blob}\t999.2_future_note\n"
    return git(w["author"], "mktree", input=entries)


@pytest.mark.parametrize("variant", ["noncurrent-name", "unknown-entry"])
def test_show_and_verify_decode_by_number_preserving_tree_identity(record_world, variant):
    w = record_world
    tree = altered_tree(w, variant)
    commit = attach(w["author"], tree)
    assert tree != w["tree"], "fixture mutation must change the stored tree"
    fd.verify_tree(w["author"], tree)
    before = git(w["author"], "ls-tree", "-r", tree)
    got = read(grip.show_review_commit, w["author"], commit)
    assert got["members"][0]["title"] == TITLE.rstrip("\n")
    assert read(grip.verify_review_commit, w["author"], commit)["stored_tree"] == tree
    assert git(w["author"], "ls-tree", "-r", commit + "^{tree}") == before
    assert ref_targets(w["author"]) == {review_ref(commit): commit}


@pytest.mark.parametrize("variant", ["noncurrent-name", "unknown-entry"])
def test_receive_preserves_unknown_objects_and_noncurrent_names(record_world, variant):
    w = record_world
    tree = altered_tree(w, variant)
    commit = attach(w["author"], tree)
    before = git(w["author"], "ls-tree", "-r", tree)
    git(w["author"], "push", w["remote"], f"{commit}:{review_ref(commit)}")
    got = read(grip.receive_review_commit, w["receiver"], commit, w["remote"], ref=review_ref(commit))
    assert got["id"] == "gr:" + commit
    assert git(w["receiver"], "ls-tree", "-r", commit + "^{tree}") == before
    assert ref_targets(w["receiver"]) == {got["ref"]: commit}


def test_optional_absent_fields_show_and_verify_without_inventing_content(record_world, tmp_path):
    w = record_world
    member = dict(w["record"]["members"][0])
    for key in ("head_tree", "metadata", "range_patch", "committers", "evidence"):
        member.pop(key)
    tree = fd.write_record(w["author"], {**w["record"], "members": [member]})
    commit = attach(w["author"], tree)
    got = read(grip.show_review_commit, w["author"], commit)
    assert got["members"][0]["files"] is None
    assert read(grip.verify_review_commit, w["author"], commit)["tree_matches"] is True
    with pytest.raises(grip.GripReviewRefused) as exc:
        grip.reconstruct_review_lane(w["author"], commit, "recall", tmp_path / "no-carried-range")
    assert exc.value.refusal == "row_carries_no_objects"
    assert not (tmp_path / "no-carried-range").exists()


def test_absent_evidence_gives_no_checks_and_absent_committers_is_tree_faithful(record_world, tmp_path):
    w = record_world
    member = dict(w["record"]["members"][0])
    for key in ("metadata", "committers", "evidence"):
        member.pop(key)
    tree = fd.write_record(w["author"], {**w["record"], "members": [member]})
    commit = attach(w["author"], tree)
    got = read(grip.run_review_checks, w["author"], commit, "recall", tmp_path / "optional-lane")
    assert got["runs"] == []
    assert got["materialized"]["reconstructed_tree"] == member["head_tree"]


def test_legacy_layout_reads_and_automatic_read_does_not_convert_ids(record_world, tmp_path):
    w = record_world
    row = _row(w["remote"], w["base"], w["head"])
    row.update(source=str(w["work"]), evidence=EVIDENCE.decode())
    commit = grip.create_review_bind_commit(w["author"], [row])
    before = ref_targets(w["author"])
    assert before == {legacy_review_ref(commit): commit}
    tree = git(w["author"], "rev-parse", commit + "^{tree}")
    content = git(w["author"], "cat-file", "-p", commit)
    shown = grip.show_review_commit(w["author"], commit)
    assert shown["id"] == "gr:" + commit and shown["members"][0]["head"] == w["head"]
    assert grip.verify_review_commit(w["author"], commit)["stored_tree"] == tree
    got = grip.run_review_checks(w["author"], commit, "recall", tmp_path / "legacy-lane")
    assert got["runs"][0]["exit_matched"] is True
    assert got["materialized"]["reconstructed_tree"] == w["record"]["members"][0]["head_tree"]
    assert ref_targets(w["author"]) == before, "a read converted the legacy record or its id"
    assert git(w["author"], "cat-file", "-p", commit) == content


def test_automatic_legacy_store_read_keeps_original_ids_and_record_bytes(two_member_ws, tmp_path):
    from tests.test_review_bind_migration import _legacy_binds
    from tests.test_store_break_attempts import _cli
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    commits = _legacy_binds(ws, tmp_path)
    legacy = ws / ".grip"
    original = {commit: (git(legacy, "cat-file", "-p", commit),
                         git(legacy, "rev-parse", commit + "^{tree}")) for commit in commits}
    for commit in commits:
        shown = grip.show_review_commit(ws, commit)
        assert shown["id"] == "gr:" + commit, "automatic read changed the record id"
        assert grip.verify_review_commit(ws, commit)["stored_tree"] == original[commit][1]
        assert git(ws, "cat-file", "-p", commit) == original[commit][0]
    assert ref_targets(ws) == {legacy_review_ref(commit): commit for commit in commits}
    assert (ws / ".grip" / "legacy-store.git").is_dir()
    assert not (ws / ".grip" / ".git").exists()
    # Repeated read remains a no-op, including the ID displayed to the caller.
    assert grip.show_review_commit(ws, commits[0])["id"] == "gr:" + commits[0]
    assert ref_targets(ws) == {legacy_review_ref(commit): commit for commit in commits}

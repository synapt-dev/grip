"""Edges of the field tree readers that the reader red set does not pin: a record that fails
verify is refused by every reader, publish keeps the spelling a bind already has, and
receive refuses when another spelling of the same id names a different commit."""
from __future__ import annotations

import pytest

from gr2.python_cli import grip
from gr2.python_cli import review_field_tree as fd
from tests.review_ref_helper import legacy_review_ref, review_ref
from tests.test_review_field_tree_readers import attach, bound, record_world, ref_targets  # noqa: F401
from tests.test_review_transport import git
from tests.test_store_break_attempts import two_member_ws  # noqa: F401


def _duplicate_kind(root, tree):
    """The record with a second entry for field 2: a tree verify refuses."""
    blob = git(root, "hash-object", "-w", "--stdin", input="review")
    entries = git(root, "ls-tree", tree) + f"\n100644 blob {blob}\t002.2_kind_again\n"
    return git(root, "mktree", input=entries)


def test_readers_refuse_a_field_tree_record_that_fails_verify(record_world, tmp_path):
    w = record_world
    assert grip.show_review_commit(w["author"], attach(w["author"], w["tree"]))["members"]  # control
    bad = _duplicate_kind(w["author"], w["tree"])
    commit = git(w["author"], "commit-tree", bad, "-m", "malformed")
    git(w["author"], "update-ref", review_ref(commit), commit, "0" * 40)
    for call, args in ((grip.show_review_commit, ()), (grip.verify_review_commit, ()),
                       (grip.review_row_keys, ()),
                       (grip.reconstruct_review_lane, ("recall", tmp_path / "lane"))):
        with pytest.raises(grip.GripCorruptError, match="invalid field tree review record"):
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


@pytest.mark.parametrize("key", ["../escape", "/tmp/pwn", "a/b", ".", "..", ".git", ".venv", ".grip-review-open.json",
                                 ".grip-review-run.json", "a\\b", "a\nb", "a\0b"])
def test_a_member_key_that_is_not_a_plain_name_is_refused_before_any_path(record_world, tmp_path, key):
    w = record_world
    commit = _bound_variant(w, key=key)
    for call, args in ((grip.show_review_commit, ()), (grip.verify_review_commit, ()), (grip.review_row_keys, ()),
                       (grip.reconstruct_review_lane, (key, tmp_path / "lane"))):
        with pytest.raises(grip.GripCorruptError, match="member key"):
            call(w["author"], commit, *args)
    assert not (tmp_path / "lane").exists()


@pytest.mark.parametrize("key", ["alpha", ".github-org", ".github", "a.b"])
def test_a_plain_member_key_is_read(record_world, key):
    commit = _bound_variant(record_world, key=key)
    assert grip.review_row_keys(record_world["author"], commit) == [key]


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


def _bound_record(w, record):
    tree = fd.write_record(w["author"], record)
    commit = git(w["author"], "commit-tree", tree, "-m", "record")
    git(w["author"], "update-ref", review_ref(commit), commit, "0" * 40)
    return commit


def test_a_record_of_another_schema_or_kind_is_not_a_bind(record_world):
    w = record_world
    for change in ({"schema": "something-else"}, {"kind": "project"}):
        commit = _bound_record(w, {**w["record"], **change})
        with pytest.raises(grip.GripCorruptError, match="not a gr2 review bind commit"):
            grip.show_review_commit(w["author"], commit)


def test_a_bind_with_no_member_is_refused(record_world):
    commit = _bound_record(record_world, {**record_world["record"], "members": []})
    with pytest.raises(grip.GripCorruptError, match="names no member"):
        grip.verify_review_commit(record_world["author"], commit)


@pytest.mark.parametrize("field,value,reason", [
    ("commit", "HEAD", "not a commit id"), ("base", "1" * 39, "not a commit id"),
    ("remote", "--upload-pack=x", "remote is not a remote"), ("remote", "https://x\x01y", "remote is not a remote"),
])
def test_repository_values_that_are_not_ids_or_remotes_are_refused(record_world, field, value, reason):
    commit = _bound_variant(record_world, **{field: value})
    with pytest.raises(grip.GripCorruptError, match=reason):
        grip.verify_review_commit(record_world["author"], commit)


@pytest.mark.parametrize("keep", [("metadata",), ("head_tree",), ("range_patch",), ("metadata", "committers")])
def test_reconstruction_needs_a_range_and_its_tree_before_any_clone(record_world, tmp_path, keep):
    w = record_world
    carried = ("range_patch", "metadata", "head_tree", "committers")
    commit = _bound_variant(w, **{k: None for k in carried if k not in keep})
    with pytest.raises(grip.GripReviewRefused) as exc:
        grip.reconstruct_review_lane(w["author"], commit, "recall", tmp_path / "lane")
    assert exc.value.refusal == "row_carries_no_objects"
    assert not (tmp_path / "lane").exists()


def test_text_that_is_not_utf8_is_refused_by_name(record_world, tmp_path):
    """A title that is not UTF-8 (a string field), and a range that is not UTF-8 (bytes the lane applies)."""
    w = record_world
    member = {**w["record"]["members"][0]}
    raw = fd.encode({**w["record"], "members": [member]})
    bad_title = raw.replace(member["title"].encode(), b"\xff\xfe" + member["title"].encode()[2:], 1)
    bad_range = raw.replace(member["range_patch"], member["range_patch"].replace(b"From", b"Fr\xffm", 1), 1)
    for raw_bad in (bad_title, bad_range):
        tree = fd.write_tree(w["author"], raw_bad)
        commit = git(w["author"], "commit-tree", tree, "-m", "not utf-8")
        git(w["author"], "update-ref", review_ref(commit), commit, "0" * 40)
        for call, args in ((grip.show_review_commit, ()), (grip.verify_review_commit, ()),
                           (grip.reconstruct_review_lane, ("recall", tmp_path / "lane"))):
            with pytest.raises(grip.GripCorruptError, match="not UTF-8"):
                call(w["author"], commit, *args)
    assert not (tmp_path / "lane").exists()


@pytest.mark.parametrize("bad_key", ["../escape", "/abs", ".", ".git", ".venv", ".grip-review-open.json"])
def test_review_open_refuses_a_bad_member_key_before_any_member_is_reconstructed(
        record_world, tmp_path, monkeypatch, bad_key):
    from tests.test_pr_review_subject import gr2
    w = record_world
    first = {**w["record"]["members"][0], "key": "alpha", "path": "alpha"}
    second = {**w["record"]["members"][0], "key": bad_key, "path": "beta"}
    commit = _bound_record(w, {**w["record"], "members": [first, second]})
    calls = []
    monkeypatch.setattr(grip, "reconstruct_review_lane", lambda *a, **k: calls.append(a))
    lane = tmp_path / "lane"
    result = gr2(w["author"], monkeypatch, "review", "open", "gr:" + commit, "--lane-dir", lane)
    assert result.exit_code != 0
    assert "member key" in result.output + str(result.exception)
    assert calls == []
    assert not lane.exists() or not any(lane.iterdir())
    good = _bound_record(w, {**w["record"], "members": [first, {**second, "key": "beta"}]})
    assert grip.review_row_keys(w["author"], good) == ["alpha", "beta"]  # control


@pytest.mark.parametrize("bad_key", ["..", ".git"])
def test_a_legacy_bind_with_an_unsafe_member_key_is_refused(record_world, bad_key):
    """Legacy keys were Git tree entry names, and git accepts ".." there: the same rule applies."""
    from tests.test_review_bind_verify import _row
    from tests.test_review_field_tree_readers import EVIDENCE
    w = record_world
    row = _row(w["remote"], w["base"], w["head"])
    row.update(source=str(w["work"]), evidence=EVIDENCE.decode())
    good = grip.create_review_bind_commit(w["author"], [row])
    assert grip.review_row_keys(w["author"], good)  # control

    def renamed(tree, sub):
        rows = git(w["author"], "ls-tree", f"{tree}:{sub}").splitlines()
        return git(w["author"], "mktree", input="".join(
            line.rsplit("\t", 1)[0] + "\t" + bad_key + "\n" for line in rows))

    root = git(w["author"], "rev-parse", good + "^{tree}")
    top = []
    for line in git(w["author"], "ls-tree", root).splitlines():
        meta, name = line.split("\t")
        if name in ("repos", "observed", "texts", "objects", "evidence"):
            meta = f"040000 tree {renamed(root, name)}"
        top.append(f"{meta}\t{name}\n")
    bad = git(w["author"], "commit-tree", git(w["author"], "mktree", input="".join(top)), "-m", "legacy bad key")
    git(w["author"], "update-ref", legacy_review_ref(bad), bad, "0" * 40)
    with pytest.raises(grip.GripCorruptError, match="member key"):
        grip.review_row_keys(w["author"], bad)


def test_verify_measures_the_store_now_not_a_cached_view(bound):
    """A long-lived caller views a bind, the store changes underneath, and verify must see it."""
    author = bound["author"]
    assert grip.show_review_commit(author, bound["id"])["members"]  # fills the readers' cache
    blob = git(author, "rev-parse", bound["id"] + ":004.2r_members/0000001/010.2_title")
    loose = author / ".git" / "objects" / blob[:2] / blob[2:]
    assert loose.is_file()
    import zlib
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"blob 3\0bad"))
    with pytest.raises(grip.GripCorruptError, match="fsck"):
        grip.verify_review_commit(author, bound["id"])


def test_review_open_checks_every_member_carries_a_range_before_the_first_clone(record_world, tmp_path, monkeypatch):
    from tests.test_pr_review_subject import gr2
    w = record_world
    first = {**w["record"]["members"][0], "key": "alpha", "path": "alpha"}
    second = {k: v for k, v in {**w["record"]["members"][0], "key": "beta", "path": "beta"}.items() if k != "head_tree"}
    commit = _bound_record(w, {**w["record"], "members": [first, second]})
    calls = []
    monkeypatch.setattr(grip, "reconstruct_review_lane", lambda *a, **k: calls.append(a))
    lane = tmp_path / "lane"
    result = gr2(w["author"], monkeypatch, "review", "open", "gr:" + commit, "--lane-dir", lane)
    assert result.exit_code != 0
    assert "row_carries_no_objects" in result.output + str(result.exception)
    assert calls == []
    assert not lane.exists() or not any(lane.iterdir())


def test_every_lane_control_file_carries_the_refused_prefix():
    """A member key may not start with the lane control prefix; that refusal only protects the lane
    if every control file review_run writes actually starts with it."""
    from gr2.python_cli import review_run
    from gr2.python_cli.layout import LANE_CONTROL_PREFIX
    names = [review_run._MARKER_NAME, review_run._RECEIPT_NAME, review_run._OUTPUT_LOG_NAME,
             review_run._member_log_name("alpha"), review_run._member_log_name(".github")]
    assert all(n.startswith(LANE_CONTROL_PREFIX) for n in names), names
    assert not any(grip.plain_member_key(n) for n in names)
    assert not grip.plain_member_key(review_run._VENV_DIRNAME)


def test_two_members_whose_names_overlap_get_disjoint_lane_paths():
    """`alpha` and `alpha.grip-review-run.log` are both plain keys; alpha's run log must not land in
    the other member's directory."""
    from gr2.python_cli import review_run
    keys = ["alpha", "alpha.grip-review-run.log"]
    assert all(grip.plain_member_key(k) for k in keys)
    logs = [review_run._member_log_name(k) for k in keys]
    assert len(set(logs)) == 2 and not set(logs) & set(keys), logs


def test_a_member_key_named_twice_is_refused(record_world):
    w = record_world
    member = w["record"]["members"][0]
    commit = _bound_record(w, {**w["record"], "members": [member, {**member, "path": "other"}]})
    with pytest.raises(grip.GripCorruptError, match="member key"):
        grip.review_row_keys(w["author"], commit)
    single = _bound_record(w, {**w["record"], "members": [member]})
    assert grip.review_row_keys(w["author"], single) == [member["key"]]  # control


def test_publish_refuses_a_named_spelling_the_bind_does_not_have(bound):
    """The bind exists here only under v1; asking to publish the legacy spelling must not create it."""
    with pytest.raises(grip.GripCorruptError, match="review_ref_not_local"):
        grip.publish_review_commit(bound["author"], bound["id"], bound["remote"], ref=legacy_review_ref(bound["id"]))
    assert git(bound["remote"], "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/__reviews__/") == ""
    got = grip.publish_review_commit(bound["author"], bound["id"], bound["remote"], ref=review_ref(bound["id"]))
    assert got["ref"] == review_ref(bound["id"])  # control: the spelling it has

"""Two member keys a case-insensitive filesystem resolves to one directory are one member.

The duplicate checks compare `member_key_fold(key)`, not the raw bytes: `.github` and `.GITHUB` share
`<lane>/.github` on macOS and Windows. `.gİthub` folds to `.gi̇thub`, so it stays a second member."""
from __future__ import annotations

import pytest

from gr2.python_cli import grip
from gr2.python_cli import review_run
from tests.test_review_field_tree_reader_edges import _bound_record
from tests.test_review_field_tree_readers import record_world  # noqa: F401

CASE_DUPLICATES = [
    pytest.param(".github", ".GITHUB", id="dotgithub-case"),
    pytest.param(".gitx", ".GITX", id="dotgitx-case"),
]
# The bind-side checks refuse these spellings as invalid keys before any fold compare.
DUPLICATES = CASE_DUPLICATES + [
    pytest.param("alpha", "alpha.", id="trailing-dot"),
    pytest.param("alpha", "al" + chr(0x200C) + "pha", id="hfs-ignored"),
]
DISTINCT = [
    pytest.param(".github", ".github2", id="dotgithub2"),
    pytest.param("a", "b", id="a-b"),
    pytest.param(".g" + chr(0x130) + "thub", ".github", id="non-ascii-case"),
]


def _bind_two(w, first, second):
    member = w["record"]["members"][0]
    return _bound_record(w, {**w["record"], "members": [{**member, "key": first},
                                                        {**member, "key": second, "path": "other"}]})


@pytest.mark.parametrize("first,second", DUPLICATES)
def test_a_field_tree_bind_with_two_spellings_of_one_directory_is_refused(record_world, first, second):
    commit = _bind_two(record_world, first, second)
    with pytest.raises(grip.GripCorruptError, match="member key"):
        grip.review_row_keys(record_world["author"], commit)


@pytest.mark.parametrize("first,second", DISTINCT)
def test_two_keys_that_are_two_directories_still_bind_as_two_members(record_world, first, second):
    commit = _bind_two(record_world, first, second)
    assert sorted(grip.review_row_keys(record_world["author"], commit)) == sorted([first, second])


def _legacy_bind_two(w, first, second):
    """A legacy bind whose repos/observed/texts/objects/evidence subtrees name the member twice."""
    from tests.test_review_field_tree_readers import legacy_layout_bind
    from tests.review_ref_helper import legacy_review_ref
    from tests.test_review_transport import git
    good = legacy_layout_bind(w)

    def doubled(tree, sub):
        rows = git(w["author"], "ls-tree", f"{tree}:{sub}").splitlines()
        return git(w["author"], "mktree", input="".join(
            line.rsplit("\t", 1)[0] + "\t" + name + "\n" for line in rows for name in (first, second)))

    root = git(w["author"], "rev-parse", good + "^{tree}")
    top = []
    for line in git(w["author"], "ls-tree", root).splitlines():
        meta, name = line.split("\t")
        if name in ("repos", "observed", "texts", "objects", "evidence"):
            meta = f"040000 tree {doubled(root, name)}"
        top.append(f"{meta}\t{name}\n")
    bad = git(w["author"], "commit-tree", git(w["author"], "mktree", input="".join(top)), "-m", "legacy two keys")
    git(w["author"], "update-ref", legacy_review_ref(bad), bad, "0" * 40)
    return bad


@pytest.mark.parametrize("first,second", DUPLICATES)
def test_a_legacy_bind_with_two_spellings_of_one_directory_is_refused(record_world, first, second):
    bad = _legacy_bind_two(record_world, first, second)
    with pytest.raises(grip.GripCorruptError, match="member key"):
        grip._read_repo_state(record_world["author"], bad, bind=True)


@pytest.mark.parametrize("first,second", DISTINCT)
def test_a_legacy_bind_with_two_directories_reads_both(record_world, first, second):
    bad = _legacy_bind_two(record_world, first, second)
    assert sorted(grip._read_repo_state(record_world["author"], bad, bind=True)) == sorted([first, second])


def _write_two(monkeypatch, tmp_path, first, second):
    """The writer up to its second row; the remote reads are stubbed (they are not what is under test)."""
    base, head = "1" * 40, "2" * 40
    monkeypatch.setattr(grip, "_review_bind_author", lambda ws: "Tester <t@example.invalid>")
    monkeypatch.setattr(grip, "_pin_target", lambda ws, remote, ref, field: (base, "refs/heads/main"))
    monkeypatch.setattr(grip, "_remote_ref_tips", lambda ws, remote: {})
    rows = [dict(key=k, remote="https://example.invalid/r.git", path=k, head=head, base=base, ref="main")
            for k in (first, second)]
    try:
        grip._bind_review_rows_body(tmp_path, rows)
    except grip.GripCorruptError as exc:
        return str(exc)
    except Exception as exc:  # past the duplicate check: whatever the stubbed world refuses next
        return f"past: {type(exc).__name__}"
    return "past: bound"


@pytest.mark.parametrize("first,second", DUPLICATES)
def test_the_writer_refuses_two_spellings_of_one_directory(monkeypatch, tmp_path, first, second):
    assert _write_two(monkeypatch, tmp_path, first, second).startswith(("duplicate review key", "invalid review key"))


@pytest.mark.parametrize("first,second", DISTINCT)
def test_the_writer_passes_two_directories(monkeypatch, tmp_path, first, second):
    assert not _write_two(monkeypatch, tmp_path, first, second).startswith("duplicate review key")


@pytest.mark.parametrize("first,second", CASE_DUPLICATES)
def test_a_lane_marker_naming_one_directory_twice_is_refused(first, second):
    with pytest.raises(review_run.ReviewRunRefused, match="twice"):
        review_run._resolve_member_order([first, second], None)


@pytest.mark.parametrize("first,second", DISTINCT)
def test_a_lane_marker_naming_two_directories_is_kept(first, second):
    assert review_run._resolve_member_order([first, second], None) == [first, second]

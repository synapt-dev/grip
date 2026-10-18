"""Full mode: the records about a member's pin ride in its sidecar, and materialize owns the member's path.

A review bound in the root, the approvals of that review, and the check observations for the pin are carried in the
member's sidecar under ``.grip/fields/<name>/records/<pin>/<n>/``, one tree per record, so a plain `git clone` holds
them without any custom ref. Materialize marks each member path skip-worktree and sets sparse.expectFilesOutsideOfPatterns, so the
root's `git status` stays empty with the members checked out at their paths.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.test_store_full_mode_carrier import _advance, _full_store, _gitlink
from tests.test_store_git_native_smoke import configure_identity, git, gr2


def _records(root: Path, member: str, rev: str = "HEAD") -> list[str]:
    """The record trees the sidecar at ``rev`` carries for the member's pin (the gitlink), in their numbered order."""
    pin = _gitlink(root, member, rev)
    listed = git(root, "ls-tree", f"{rev}:.grip/fields/{member}/records/{pin}", check=False).stdout.splitlines()
    return [line.split()[2] for line in listed if line.split()[1] == "tree"]


def _review_bind(root: Path) -> str:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1]), "COLUMNS": "200"}
    made = subprocess.run([sys.executable, "-m", "gr2.python_cli.app", "review", "bind", str(root)],
                          cwd=root, text=True, capture_output=True, env=env)
    assert made.returncode == 0, made.stdout + made.stderr
    refs = git(root, "for-each-ref", "--format=%(objectname)", "refs/dev.synapt.grip/__reviews__/").stdout.split()
    assert len(refs) == 1, refs
    return refs[0]


def test_a_review_rides_in_the_sidecar_and_a_plain_clone_reads_it_back(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    review = _review_bind(root)
    assert gr2(root, "commit", "-m", "with review").returncode == 0
    want = git(root, "rev-parse", f"{review}^{{tree}}").stdout.strip()

    record = _records(root, "alpha")
    assert record == [want]
    beta = _records(root, "beta")
    assert beta == []   # the review names alpha only

    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    assert git(fresh, "for-each-ref", "refs/dev.synapt.grip/").stdout == ""   # the custom ref did NOT travel
    got = _records(fresh, "alpha")
    assert got == [want]
    assert git(fresh, "cat-file", "-t", want).stdout.strip() == "tree"   # the record's objects are in the clone


def test_materialize_leaves_the_root_status_empty_with_the_member_at_its_path(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    assert gr2(root, "commit", "-m", "layered").returncode == 0
    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)

    assert gr2(fresh, "materialize").returncode == 0
    assert (fresh / "alpha" / ".git").exists() and (fresh / "beta" / ".git").exists()
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    assert git(fresh, "status", "--porcelain").stdout == ""
    git(fresh, "checkout", "-q", "main")
    git(fresh, "reset", "-q", "--hard")
    assert git(fresh, "status", "--porcelain").stdout == ""
    assert (fresh / "alpha" / ".git").exists()


def test_control_without_the_setting_the_member_path_shows_modified(tmp_path):
    """The control for the test above: the same layout without sparse.expectFilesOutsideOfPatterns."""
    root = _full_store(tmp_path)
    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    assert gr2(fresh, "materialize").returncode == 0
    assert _flags(fresh, "alpha") == ["S"]
    git(fresh, "config", "sparse.expectFilesOutsideOfPatterns", "false")
    git(fresh, "sparse-checkout", "reapply")
    # The gitlink is the member's own commit now, so the path no longer shows modified; what the setting holds is the
    # skip-worktree bit on a path the sparse patterns leave out but the member's checkout fills.
    assert _flags(fresh, "alpha") == ["H"]


def _approve_as_another(root: Path, review: str) -> list[str]:
    """Approve the review under a name that authored nothing, and return the approval link trees in chain order."""
    from gr2.python_cli import approvals

    git(root, "config", "user.name", "Second Reader")
    approvals.approve(root, review)
    tip = git(root, "rev-parse", f"refs/dev.synapt.grip/__approvals__/v1/{review}").stdout.strip()
    links = git(root, "rev-list", "--first-parent", tip, f"^{review}").stdout.split()
    return [git(root, "rev-parse", f"{link}^{{tree}}").stdout.strip() for link in links]


def test_an_approval_of_the_carried_review_rides_too(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    review = _review_bind(root)
    approval_trees = _approve_as_another(root, review)
    assert len(approval_trees) == 1
    assert gr2(root, "commit", "-m", "with approval").returncode == 0
    review_tree = git(root, "rev-parse", f"{review}^{{tree}}").stdout.strip()

    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    got = _records(fresh, "alpha")
    assert got == sorted([review_tree, *approval_trees])
    assert git(fresh, "ls-tree", "--name-only", approval_trees[0]).stdout.split() == ["approval.json"]
    beta = _records(fresh, "beta")
    assert beta == []


def test_a_check_on_the_pin_rides_without_a_review_and_reads_as_a_checks_tree(tmp_path):
    from gr2.python_cli import check_records

    root = _full_store(tmp_path)
    pin = _advance(root, "alpha", push=True)
    remote = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    check_records.run_check(root / "alpha", remote, pin, "test", [sys.executable, "-c", "pass"])
    assert gr2(root, "commit", "-m", "with check").returncode == 0

    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    got = _records(fresh, "alpha")
    assert len(got) == 1
    # The occurrence is the checks tree check_records already reads: one fan-out entry, the pin's rows, all valid.
    entries = check_records._entries(fresh, got[0])
    assert list(entries) == [pin[:2] + "/" + pin[2:]]
    rows = check_records._records(git(fresh, "cat-file", "blob", entries[pin[:2] + "/" + pin[2:]]).stdout.encode(), pin)
    assert [(r["name"], r["result"]) for r in rows] == [("test", "pass")]


def test_the_records_sit_under_the_pin_in_the_members_sidecar(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    _review_bind(root)
    assert gr2(root, "commit", "-m", "with review").returncode == 0
    pin = _gitlink(root, "alpha")
    names = git(root, "ls-tree", "--name-only", f"HEAD:.grip/fields/alpha/records").stdout.split()
    assert names == [pin], names


def test_a_review_of_an_earlier_pin_is_not_carried(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    _review_bind(root)
    _advance(root, "alpha", push=False)   # the pin moves past the reviewed head
    assert gr2(root, "commit", "-m", "moved on").returncode == 0
    record = _records(root, "alpha")
    assert record == []


def test_a_ref_under_the_review_root_that_is_not_a_bind_is_skipped(tmp_path):
    root = _full_store(tmp_path)
    git(root, "update-ref", "refs/dev.synapt.grip/__reviews__/stray", "HEAD")   # a plain root commit, not a bind
    _advance(root, "alpha", push=False)
    committed = gr2(root, "commit", "-m", "stray ref beside")
    assert committed.returncode == 0, committed.stdout + committed.stderr
    record = _records(root, "alpha")
    assert record == []


def test_a_compat_store_gets_no_sparse_step(tmp_path):
    root = _full_store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text(text.replace('mode = "full"\n', "", 1), encoding="utf-8")
    git(root, "commit", "-qam", "back to compat")
    git(tmp_path, "clone", "-q", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    assert gr2(fresh, "materialize").returncode == 0
    assert git(fresh, "config", "--get", "sparse.expectFilesOutsideOfPatterns", check=False).stdout == ""
    assert git(fresh, "config", "--get", "core.sparseCheckout", check=False).stdout.strip() in ("", "false")


def _flags(root: Path, *paths: str) -> list[str]:
    return [line.split()[0] for line in git(root, "ls-files", "-v", "--", *paths).stdout.splitlines()]


def test_a_commit_in_a_materialized_root_keeps_the_member_paths_owned(tmp_path):
    root = _full_store(tmp_path)
    git(tmp_path, "clone", "-q", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    assert gr2(fresh, "materialize").returncode == 0
    assert _flags(fresh, "alpha", "beta") == ["S", "S"]

    assert gr2(fresh, "commit", "-m", "nothing new").returncode == 0
    assert _flags(fresh, "alpha", "beta") == ["S", "S"]
    assert git(fresh, "status", "--porcelain").stdout == ""

    _advance(fresh, "alpha", push=True)
    moved = gr2(fresh, "commit", "-m", "alpha moved")
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert _flags(fresh, "alpha", "beta") == ["S", "S"]
    assert git(fresh, "status", "--porcelain").stdout == ""


def test_a_commit_in_a_root_never_materialized_sets_no_skip_worktree(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=False)
    assert gr2(root, "commit", "-m", "plain root").returncode == 0
    assert _flags(root, "alpha", "beta") == ["H", "H"]


def _root_with_a_check_on_alpha(tmp_path: Path) -> tuple[Path, str]:
    from gr2.python_cli import check_records

    root = _full_store(tmp_path)
    pin = _advance(root, "alpha", push=True)
    remote = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    check_records.run_check(root / "alpha", remote, pin, "test", [sys.executable, "-c", "pass"])
    assert gr2(root, "commit", "-m", "with check").returncode == 0
    return root, _gitlink(root, "alpha")


def test_a_clone_that_moves_another_member_keeps_this_members_records(tmp_path):
    """A clone holds neither the root's review refs nor a member's check ref; the records already carried for an
    unchanged pin must survive a commit made there."""
    root, alpha_entry = _root_with_a_check_on_alpha(tmp_path)
    git(tmp_path, "clone", "-q", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    assert gr2(fresh, "materialize").returncode == 0
    configure_identity(fresh / "beta")
    _advance(fresh, "beta", push=True)

    moved = gr2(fresh, "commit", "-m", "beta moved")

    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert _gitlink(fresh, "alpha") == alpha_entry
    record = _records(fresh, "alpha")
    assert len(record) == 1


def test_a_shallow_clone_keeps_the_records_because_the_sidecar_is_in_the_commit_it_has(tmp_path):
    """The field-commit shape had to refuse here (the records sat in a parent a depth-1 clone lacks). The sidecar is in
    HEAD's own tree, so the records are seen and kept, and nothing is dropped."""
    root, alpha_entry = _root_with_a_check_on_alpha(tmp_path)
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{root}", "shallow")
    shallow = tmp_path / "shallow"
    configure_identity(shallow)
    for name in ("alpha", "beta"):
        git(tmp_path, "clone", "-q", git(root / name, "remote", "get-url", "origin").stdout.strip(), str(shallow / name))
        configure_identity(shallow / name)
    _advance(shallow, "beta", push=True)

    moved = gr2(shallow, "commit", "-m", "from a shallow clone", check=False)

    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert _gitlink(shallow, "alpha") == alpha_entry
    assert _records(shallow, "alpha") == _records(root, "alpha") and len(_records(root, "alpha")) == 1


def _git_at_least(tmp_path: Path, major: int, minor: int) -> bool:
    import re
    found = re.findall(r"\d+", git(tmp_path, "version").stdout)[:2]
    return tuple(int(n) for n in found) >= (major, minor)


def test_a_partial_clone_refuses_rather_than_drop_the_records_it_cannot_read(tmp_path):
    """A blob:none clone holds the field-commit but not its record's blobs. The record read refuses the absent blob
    instead of fetching it, and the commit must refuse too: treating it as "not a record" would carry the member's
    field-commit forward with its records dropped."""
    import pytest

    if not _git_at_least(tmp_path, 2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    root, _ = _root_with_a_check_on_alpha(tmp_path)
    git(root, "config", "uploadpack.allowFilter", "true")
    git(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{root}", "partial")
    partial = tmp_path / "partial"
    configure_identity(partial)
    for name in ("alpha", "beta"):
        git(tmp_path, "clone", "-q", git(root / name, "remote", "get-url", "origin").stdout.strip(), str(partial / name))
        configure_identity(partial / name)
    before = _gitlink(partial, "alpha")
    _advance(partial, "beta", push=True)

    refused = gr2(partial, "commit", "-m", "from a partial clone", check=False)

    out = refused.stdout + refused.stderr
    assert refused.returncode == 4, out
    assert "is missing locally" in out and " fetch -- origin " in out
    assert "Traceback" not in out
    assert _gitlink(partial, "alpha") == before   # nothing was committed over alpha's record
    records = mft.read(root, git(root, "rev-parse", f"{_gitlink(root, 'alpha')}^{{tree}}").stdout.strip())["records"]
    assert len(records) == 1   # and the root still carries alpha's check


def test_a_partial_clone_names_the_absent_record_when_reading_a_members_pin(tmp_path):
    """The pin reader of a full-mode root reads the gitlink through the field record. In a blob:none clone that read
    is refused, and the reader says so rather than reporting that the commit gives no pin."""
    import pytest
    from gr2.python_cli import grip_cli

    if not _git_at_least(tmp_path, 2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    root, _ = _root_with_a_check_on_alpha(tmp_path)
    git(root, "config", "uploadpack.allowFilter", "true")
    git(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{root}", "partial")
    partial = tmp_path / "partial"

    with pytest.raises(grip_cli.NativeStoreRefusal) as refused:
        grip_cli._member_record(partial, "alpha", None, True)
    assert "is missing locally" in str(refused.value)
    assert grip_cli._member_record(root, "alpha", None, True)[1] == "gitlink"   # control: the full root reads it


def test_a_shallow_partial_clone_keeps_the_unshallow_advice_for_a_record_it_does_not_reach(tmp_path):
    """A clone that is both shallow and partial does not hold the field-commit at all. That is a shallow clone's
    absence, so the refusal names `git fetch --unshallow`, not a fetch of an unresolved `<commit>^{tree}`."""
    import pytest
    from gr2.python_cli import grip_cli

    if not _git_at_least(tmp_path, 2, 44):
        pytest.skip("git before 2.44 ignores GIT_NO_LAZY_FETCH")
    root, _ = _root_with_a_check_on_alpha(tmp_path)
    git(root, "config", "uploadpack.allowFilter", "true")
    git(tmp_path, "clone", "-q", "--depth", "1", "--filter=blob:none", f"file://{root}", "both")
    both = tmp_path / "both"

    with pytest.raises(grip_cli.NativeStoreRefusal) as refused:
        grip_cli._field_record(both, {"name": "alpha", "path": "alpha"}, "HEAD")
    assert "--unshallow" in str(refused.value)
    assert "is missing locally" not in str(refused.value) and " fetch -- " not in str(refused.value)

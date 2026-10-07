"""Explicit format migration adds a new record; a read never performs it.

The legacy trees are independent of the current writer. Alpha fixture setup
completes only the existing legacy relabel, retaining alpha snapshots. We
measure the read AFTER that setup, so conversion cannot hide behind legal copy.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.review_ref_helper import REVIEW_REF_ROOT, review_ref, legacy_review_ref, legacy_bind_tree
from gr2.python_cli import grip
from gr2.python_cli import review_form_d as fd
from gr2.python_cli.app import app
from tests.native_root_helper import native_root
from tests.test_review_transport import git, handoff, root_owned_wrong_target  # noqa: F401

runner = CliRunner()


def refs(root: Path) -> dict[str, str]:
    return dict(line.split() for line in git(
        root, "for-each-ref", "--format=%(refname) %(objectname)", REVIEW_REF_ROOT
    ).splitlines())


def object_set(root: Path) -> set[str]:
    return set(git(root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines())


def state(world):
    root = world["root"]
    out = {"root_refs": refs(root), "root_objects": object_set(root)}
    if world["shape"] == "alpha":
        out.update(source_refs=refs(world["source"]), source_objects=object_set(world["source"]))
    return out


@pytest.fixture(params=["native", "alpha"])
def legacy_world(handoff, tmp_path, monkeypatch, request):
    author, _, remote, _, base, head = handoff
    root = native_root(tmp_path / "legacy-review-root")
    shape = request.param
    source = root if shape == "native" else root / ".grip"
    if shape == "alpha":
        source.mkdir()
        git(source, "init", "-q", "-b", "main")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head,
               title="legacy title", body="legacy body")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Legacy Fixture")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "legacy@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2020-01-02T03:04:05Z")
    tree = legacy_bind_tree(source, row)
    commit = git(source, "commit-tree", tree, "-m", "legacy bind message")
    if shape == "native":
        git(root, "update-ref", legacy_review_ref(commit), commit)
    else:
        # A snapshot above the bind keeps the alpha store in place after the
        # legal legacy-only copy. It is not a ReviewBind format conversion.
        schema = git(source, "hash-object", "-w", "--stdin", input="gr2-workspace/v1")
        meta = git(source, "mktree", input=f"100644 blob {schema}\tschema\n")
        snapshot_tree = git(source, "mktree", input=f"040000 tree {meta}\t.grip\n")
        snapshot = git(source, "commit-tree", snapshot_tree, "-p", commit, "-m", "alpha snapshot")
        git(source, "update-ref", "HEAD", snapshot)
        grip._migrate_legacy_binds(root)
        assert (source / ".git").is_dir(), "setup must retain alpha snapshots"
        assert git(source, "rev-parse", "HEAD") == snapshot
    assert refs(root) == {legacy_review_ref(commit): commit}
    assert git(root, "show", commit + ":.grip/schema") == "gr2-review-bind/v2"
    return dict(root=root, source=source, shape=shape, old=commit, old_tree=tree,
                old_bytes=git(root, "cat-file", "-p", commit), row=row)


def migrate_rows(root: Path, monkeypatch):
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["store", "migrate-reviews", str(root), "--json"])
    assert result.exit_code == 0, f"explicit review migration refused: {result.output}"
    payload = json.loads(result.stdout)
    def rows(value):
        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
            if all({"old_id", "new_id", "ref", "status"} <= set(v) for v in value):
                return value
        if isinstance(value, dict):
            for child in value.values():
                answer = rows(child)
                if answer is not None:
                    return answer
        return None
    answer = rows(payload)
    assert answer is not None, "receipt must list old_id/new_id/ref/status migration rows"
    return answer


def full_id(value: str) -> str:
    value = value.removeprefix("gr:")
    assert len(value) == 40 and all(c in "0123456789abcdef" for c in value)
    return value


def one_mapping(world, rows):
    assert len(rows) == 1
    row = rows[0]
    assert full_id(row["old_id"]) == world["old"]
    new = full_id(row["new_id"])
    assert new != world["old"], "format conversion must create a new commit/id"
    assert row["ref"] == review_ref(new)
    assert row["status"] in ("created", "present")
    return new


def test_read_preserves_both_review_ref_and_object_sets(legacy_world):
    w = legacy_world
    before = state(w)
    shown = grip.show_review_commit(w["root"], w["old"])
    assert shown["id"] == "gr:" + w["old"]
    assert grip.verify_review_commit(w["root"], w["old"])["stored_tree"] == w["old_tree"]
    assert state(w) == before, "a read added/removed a review ref or object (format conversion)"
    assert git(w["root"], "cat-file", "-p", w["old"]) == w["old_bytes"]


def test_explicit_migrate_adds_one_v1_form_d_ref_as_the_read_control(legacy_world, monkeypatch):
    w = legacy_world
    before = state(w)
    new = one_mapping(w, migrate_rows(w["root"], monkeypatch))
    assert refs(w["root"]) == {**before["root_refs"], review_ref(new): new}
    assert object_set(w["root"]) > before["root_objects"], "explicit conversion did not add a D tree/commit"
    tree = git(w["root"], "rev-parse", new + "^{tree}")
    assert tree != w["old_tree"]
    fd.verify_tree(w["root"], tree)
    assert fd.read_record(w["root"], tree)["members"][0]["commit"] == w["row"]["head"]
    if w["shape"] == "alpha":
        assert refs(w["source"]) == before["source_refs"]
        assert object_set(w["source"]) == before["source_objects"]


def test_migrate_keeps_legacy_targets_trees_bytes_and_receipt_outside_review_namespace(legacy_world, monkeypatch):
    w = legacy_world
    before = state(w)
    new = one_mapping(w, migrate_rows(w["root"], monkeypatch))
    assert refs(w["root"]) == {**before["root_refs"], review_ref(new): new}, "legacy ref deleted/rewritten or a receipt ref was published"
    assert git(w["root"], "rev-parse", legacy_review_ref(w["old"])) == w["old"]
    assert git(w["root"], "rev-parse", w["old"] + "^{tree}") == w["old_tree"]
    assert git(w["root"], "cat-file", "-p", w["old"]) == w["old_bytes"]
    receipts = list((w["root"] / ".grip" / "receipts").rglob("*.json"))
    assert receipts, "the old-to-new receipt must be a file under .grip/receipts"
    def mapping_present(value):
        if isinstance(value, dict):
            normalized = {str(k).removeprefix("gr:"): str(v).removeprefix("gr:") for k,v in value.items()}
            if normalized.get(w["old"]) == new:
                return True
            vals = set(normalized.values())
            if w["old"] in vals and new in vals:
                return True
            return any(mapping_present(v) for v in value.values())
        if isinstance(value, list):
            return any(mapping_present(v) for v in value)
        return False
    assert any(mapping_present(json.loads(p.read_text())) for p in receipts), "durable receipt lost the old-to-new mapping"
    assert set(c for c, _ in grip.list_review_binds(w["root"])) == {w["old"], new}
    if w["shape"] == "alpha":
        assert refs(w["source"]) == before["source_refs"]
        assert object_set(w["source"]) == before["source_objects"]


def test_migrate_id_is_stable_across_caller_identity_clock_and_rerun(legacy_world, monkeypatch, tmp_path):
    w = legacy_world
    clone = tmp_path / "same-record-other-clock"
    shutil.copytree(w["root"], clone)
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "First Caller")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "first@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2030-01-01T00:00:00Z")
    first_rows = migrate_rows(w["root"], monkeypatch)
    first = one_mapping(w, first_rows)
    assert first_rows[0]["status"] == "created"
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Different Caller")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "second@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2040-02-03T04:05:06Z")
    second_rows = migrate_rows(clone, monkeypatch)
    assert one_mapping(w, second_rows) == first, "migration used caller identity/clock rather than legacy commit inputs"
    second_commit = git(clone, "cat-file", "-p", first)
    assert second_commit == git(w["root"], "cat-file", "-p", first)
    for root in (w["root"], clone):
        again = migrate_rows(root, monkeypatch)
        assert one_mapping(w, again) == first
        assert again[0]["status"] == "present"
        assert refs(root) == {legacy_review_ref(w["old"]): w["old"], review_ref(first): first}


def test_migrate_present_v1_ref_at_wrong_target_refuses_without_touching_legacy(legacy_world, monkeypatch):
    w = legacy_world
    new = one_mapping(w, migrate_rows(w["root"], monkeypatch))
    wrong = root_owned_wrong_target(w["root"], new)
    git(w["root"], "update-ref", review_ref(new), wrong)
    before = state(w)
    monkeypatch.chdir(w["root"])
    result = runner.invoke(app, ["store", "migrate-reviews", str(w["root"]), "--json"])
    assert result.exit_code != 0 and "review_ref_target_mismatch" in result.output, result.output
    assert state(w) == before, "refused migration changed refs or objects"
    assert git(w["root"], "cat-file", "-p", w["old"]) == w["old_bytes"]


def test_plain_store_migrate_still_refuses_native_root(legacy_world, monkeypatch):
    w = legacy_world
    before = state(w)
    monkeypatch.chdir(w["root"])
    result = runner.invoke(app, ["store", "migrate", "--json"])
    reason = "native store already exists" if w["shape"] == "alpha" else "no alpha .grip/.git store"
    assert result.exit_code == 4 and reason in result.output, result.output
    assert state(w) == before

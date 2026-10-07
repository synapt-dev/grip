"""Explicit format migration adds a new record; a read never performs it.

The legacy trees are independent of the current writer. Alpha fixture setup
completes only the existing legacy relabel, retaining alpha snapshots. We
measure the read AFTER that setup, so conversion cannot hide behind legal copy.
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


def migrate_rows(root: Path, monkeypatch, receipt_out=None):
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["store", "migrate-reviews", str(root), "--json"])
    assert result.exit_code == 0, f"explicit review migration refused: {result.output}"
    payload = json.loads(result.stdout)
    assert set(payload) == {"receipt", "rows"}, "migration JSON contract is receipt + rows"
    assert isinstance(payload["rows"], list)
    for row in payload["rows"]:
        assert set(row) == {"old_id", "new_id", "ref", "status"}
    receipt = Path(payload["receipt"])
    if not receipt.is_absolute():
        receipt = root / receipt
    assert receipt.resolve().is_relative_to((root / ".grip" / "receipts").resolve())
    assert receipt.is_file(), "returned receipt path does not exist"
    if receipt_out is not None:
        receipt_out.append(receipt)
    return payload["rows"]


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
    row = w["row"]
    assert fd.read_record(w["root"], tree) == {
        "schema": grip._REVIEW_BIND_SCHEMA, "kind": "review", "policy": "no-policy",
        "members": [{"key": row["key"], "path": row["path"], "remote": row["remote"],
                     "base": row["base"], "commit": row["head"], "remote_head": row["base"],
                     "title": row["title"], "body": row["body"]}]}, "SHA-only legacy content changed or optional objects invented"
    if w["shape"] == "alpha":
        assert refs(w["source"]) == before["source_refs"]
        assert object_set(w["source"]) == before["source_objects"]


def test_migrate_keeps_legacy_targets_trees_bytes_and_receipt_outside_review_namespace(legacy_world, monkeypatch):
    w = legacy_world
    before = state(w)
    receipt_paths = []
    returned_rows = migrate_rows(w["root"], monkeypatch, receipt_paths)
    new = one_mapping(w, returned_rows)
    assert refs(w["root"]) == {**before["root_refs"], review_ref(new): new}, "legacy ref deleted/rewritten or a receipt ref was published"
    assert git(w["root"], "rev-parse", legacy_review_ref(w["old"])) == w["old"]
    assert git(w["root"], "rev-parse", w["old"] + "^{tree}") == w["old_tree"]
    assert git(w["root"], "cat-file", "-p", w["old"]) == w["old_bytes"]
    durable = json.loads(receipt_paths[0].read_text())
    assert isinstance(durable, dict) and isinstance(durable.get("rows"), list)
    assert durable["rows"] == returned_rows, "receipt rows differ from returned mappings"
    assert durable["rows"] == [{"old_id": returned_rows[0]["old_id"],
                                "new_id": returned_rows[0]["new_id"],
                                "ref": review_ref(new), "status": "created"}]
    assert full_id(durable["rows"][0]["old_id"]) == w["old"]
    assert full_id(durable["rows"][0]["new_id"]) == new
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
    assert commit_inputs(w["root"], first) == commit_inputs(w["root"], w["old"])
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


def commit_inputs(root, commit):
    raw = subprocess.run(["git", "-C", str(root), "cat-file", "-p", commit],
                         capture_output=True, check=True).stdout
    headers, _, message = raw.partition(b"\n\n")
    return ([line for line in headers.splitlines() if line.startswith((b"author ", b"committer "))], message)


def test_migrate_preserves_two_distinct_legacy_commit_inputs(legacy_world, monkeypatch):
    w = legacy_world
    for who, name, date in (("AUTHOR", "Second Author", "2021-06-07T08:09:10+02:00"),
                            ("COMMITTER", "Second Committer", "2022-07-08T09:10:11-04:00")):
        monkeypatch.setenv(f"GIT_{who}_NAME", name)
        monkeypatch.setenv(f"GIT_{who}_EMAIL", name.lower().replace(" ", ".") + "@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", date)
    second = git(w["root"], "commit-tree", w["old_tree"], "-m", "second subject\n\nFull second body.\nAnother paragraph.\n")
    git(w["root"], "update-ref", legacy_review_ref(second), second)
    originals = {old: commit_inputs(w["root"], old) for old in (w["old"], second)}
    assert originals[w["old"]] != originals[second]
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Unrelated Caller")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "caller@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2040-01-01T00:00:00Z")
    rows = migrate_rows(w["root"], monkeypatch)
    assert {full_id(row["old_id"]) for row in rows} == set(originals)
    for row in rows:
        old, new = full_id(row["old_id"]), full_id(row["new_id"])
        assert commit_inputs(w["root"], new) == originals[old], "migration changed source author/committer/dates/full message"
        assert row["ref"] == review_ref(new) and row["status"] == "created"
        assert git(w["root"], "rev-parse", legacy_review_ref(old)) == old


def test_migrate_rich_legacy_record_preserves_full_content_and_sorted_members(handoff, tmp_path, monkeypatch):
    author, _, remote, _, base, head = handoff
    root = native_root(tmp_path / "rich-legacy")
    work = author / "member"
    def raw(*args):
        return subprocess.run(["git", "-C", str(work), *args], capture_output=True, check=True).stdout.decode()
    objects = {"range.patch": raw("format-patch", "--stdout", base + ".." + head),
               "metadata": raw("log", "--format=fuller", base + ".." + head),
               "head-tree": git(work, "rev-parse", head + "^{tree}"),
               "committers": raw("log", "--reverse", "--format=%cn%x09%ce%x09%cI", base + ".." + head)}
    rows = [dict(key=key, path=key, remote=str(remote), base=base, head=head, remote_head=base,
                 title=key + " title\n\n", body=key + " body\ncontinued\n\n", policy="passed",
                 objects=objects, evidence="label: " + key + "\ncommand: true\nexit: 0\n",
                 resolution=key + " resolution\n") for key in ("zeta", "alpha")]
    expected = {"schema": grip._REVIEW_BIND_SCHEMA, "kind": "review", "policy": "passed", "members": [
        {"key": r["key"], "path": r["path"], "remote": r["remote"], "base": base, "commit": head,
         "remote_head": base, "title": r["title"].rstrip("\n"), "body": r["body"].rstrip("\n"),
         "head_tree": objects["head-tree"], "metadata": objects["metadata"].encode(),
         "range_patch": objects["range.patch"].encode(), "committers": objects["committers"].encode(),
         "evidence": {"commands": r["evidence"].encode(), "resolution": r["resolution"].encode()}}
        for r in sorted(rows, key=lambda r: r["key"])]}
    tree = legacy_bind_tree(root, rows)
    old = git(root, "commit-tree", tree, "-m", "rich legacy inputs")
    git(root, "update-ref", legacy_review_ref(old), old)
    # The independent expected record can itself be written/verified as D.
    fd.verify_tree(root, fd.write_record(root, expected))
    mappings = migrate_rows(root, monkeypatch)
    new = one_mapping({"old": old}, mappings)
    actual_tree = git(root, "rev-parse", new + "^{tree}")
    fd.verify_tree(root, actual_tree)
    assert fd.read_record(root, actual_tree) == expected, "migration lost or changed carried fields/member order"
    assert git(root, "rev-parse", old + "^{tree}") == tree


def test_migrate_reviews_cli_api_contract():
    api = (Path(__file__).resolve().parents[1] / "api" / "cli.api").read_text()
    for line in ("verb  store migrate-reviews", "arg   store migrate-reviews root", "flag  store migrate-reviews --json",
                 "json  store migrate-reviews .receipt", "json  store migrate-reviews .rows[].old_id", "json  store migrate-reviews .rows[].new_id",
                 "json  store migrate-reviews .rows[].ref", "json  store migrate-reviews .rows[].status"):
        assert any(row.startswith(line + " ") for row in api.splitlines()), "missing CLI API contract: " + line

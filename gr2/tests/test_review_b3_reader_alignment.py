"""B3 producers obey final B2 reader boundaries; ordinary red-first rows.

Mutants are at the reached shared-key/seen-key checks, local-ref selection,
exact receive lookup, and each actual log writer/receipt call site. Migration
reuses the bind reader boundary and does not promise an all-binds transaction.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import grip, review_form_d as fd, review_run as rr
from gr2.python_cli.app import app
from gr2.python_cli.open_gr_review import close_open_gr_lane
from tests.native_root_helper import native_root
from tests.review_ref_helper import review_ref, legacy_review_ref, legacy_bind_tree
from tests.test_review_transport import git, cli, handoff, root_owned_wrong_target  # noqa: F401
from tests.test_review_ref_v1_writer import refs
from tests.test_review_ref_v1_migration import legacy_world, migrate_rows, full_id  # noqa: F401
from tests.test_review_run_multi_repo import HOST_PATHS, PTH_SCRIPT


def writer_row(handoff, key):
    author, _, remote, _, base, head = handoff
    return dict(key=key, path="member", remote=str(remote), base=base, head=head,
                ref="refs/heads/main", title="first", body="", source=str(author / "member"))


@pytest.mark.parametrize("key", [".venv", ".grip-review-run.log.alpha", ".grip-review-open.json",
                                 "", ".", "..", ".git", "bad/slash", "bad\\slash", "bad\0key", "bad\nkey"])
def test_new_writer_refuses_unsafe_key_before_publication(handoff, key):
    author = handoff[0]
    before = refs(author)
    failure = None
    try:
        grip.create_review_bind_commit(author, [writer_row(handoff, key)])
    except (grip.GripCorruptError, RuntimeError, ValueError) as exc:
        failure = exc
    assert refs(author) == before, f"unsafe member key {key!r} reached a durable review ref"
    assert isinstance(failure, grip.GripCorruptError) and "key" in str(failure), f"unsafe member key {key!r} needs a named refusal; got {failure!r}"


@pytest.mark.parametrize("key", [".github", "alpha", "alpha.grip-review-run.log"])
def test_new_writer_accepts_plain_names_and_preserves_them_in_form_d(handoff, key):
    author = handoff[0]
    commit = grip.create_review_bind_commit(author, [writer_row(handoff, key)])
    tree = git(author, "rev-parse", commit + "^{tree}")
    fd.verify_tree(author, tree)
    record = fd.read_record(author, tree)
    assert [(m["key"], m["title"]) for m in record["members"]] == [(key, "first")]
    assert refs(author)[review_ref(commit)] == commit


def test_new_writer_refuses_duplicate_keys_with_different_content(handoff):
    author = handoff[0]
    row = writer_row(handoff, "alpha")
    before = refs(author)
    with pytest.raises(grip.GripCorruptError, match="duplicate.*key|key.*duplicate"):
        grip.create_review_bind_commit(author, [row, dict(row, path="another", title="second")])
    assert refs(author) == before


def test_new_writer_keeps_both_distinct_members_sorted(handoff):
    author = handoff[0]
    rows = [writer_row(handoff, k) for k in ("zeta", "alpha")]
    rows[0].update(path="zeta", title="zeta content")
    rows[1].update(path="alpha", title="alpha content")
    commit = grip.create_review_bind_commit(author, rows)
    tree = git(author, "rev-parse", commit + "^{tree}")
    fd.verify_tree(author, tree)
    assert [(m["key"], m["path"], m["title"]) for m in fd.read_record(author, tree)["members"]] == [
        ("alpha", "alpha", "alpha content"), ("zeta", "zeta", "zeta content")]


def source_record(handoff, tmp_path, version):
    _, _, remote, _, base, head = handoff
    source = native_root(tmp_path / "spelling-source")
    row = dict(key="member", path="member", remote=str(remote), base=base, head=head)
    if version == "v1":
        tree = fd.write_record(source, {"schema": grip._REVIEW_BIND_SCHEMA, "kind": "review", "policy": "no-policy",
            "members": [{"key": "member", "path": "member", "remote": str(remote), "base": base,
                         "commit": head, "remote_head": base}]})
    else:
        tree = legacy_bind_tree(source, row)
    commit = git(source, "commit-tree", tree, "-m", "independent transport record")
    ref = review_ref(commit, version=version)
    git(source, "update-ref", ref, commit)
    return source, remote, commit, ref


@pytest.mark.parametrize("version", ["", "v1"], ids=["legacy-only", "v1-only"])
def test_default_publish_preserves_local_spelling_and_missing_explicit_ref_refuses(handoff, tmp_path, monkeypatch, version):
    source, remote, commit, ref = source_record(handoff, tmp_path, version)
    missing = review_ref(commit, version="" if version else "v1")
    result = cli(source, monkeypatch, "publish", "gr:" + commit, "--remote", remote, "--ref", missing)
    assert result.exit_code != 0 and "review_ref_not_local" in result.output, result.output
    assert refs(remote) == {}, "missing explicit local spelling reached remote"
    result = cli(source, monkeypatch, "publish", "gr:" + commit, "--remote", remote)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ref"] == ref
    assert refs(remote) == {ref: commit}


@pytest.mark.parametrize("fault", ["both", "wrong-target", "measurement", "malformed"])
def test_default_receive_uses_v1_once_and_never_falls_back_on_fault(handoff, tmp_path, monkeypatch, fault):
    source, remote, commit, v1 = source_record(handoff, tmp_path, "v1")
    receiver = native_root(tmp_path / "receive-target")
    target = root_owned_wrong_target(source, commit) if fault == "wrong-target" else commit
    git(source, "push", remote, f"{target}:{v1}", f"{commit}:{legacy_review_ref(commit)}")
    measured = []
    original = grip.git
    def spy(root, *args, **kwargs):
        if args and args[0] == "ls-remote":
            measured.append(tuple(map(str, args)))
            if args[-1] == v1 and fault == "measurement":
                return 1, "", "fixture cannot measure v1"
            if args[-1] == v1 and fault == "malformed":
                return 0, "not-a-valid-ls-remote-row\n", ""
        return original(root, *args, **kwargs)
    monkeypatch.setattr(grip, "git", spy)
    result = cli(receiver, monkeypatch, "receive", "gr:" + commit, "--remote", remote)
    assert measured == [("ls-remote", "--refs", str(remote), v1)], "v1 present/fault must not query legacy or a glob"
    if fault == "both":
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ref"] == v1 and refs(receiver) == {v1: commit}
    else:
        assert result.exit_code != 0, result.output
        if fault == "wrong-target":
            assert "review_ref_target_mismatch" in result.output, result.output
        if fault == "measurement":
            assert "cannot_measure_review_ref" in result.output, result.output
        assert refs(receiver) == {}


@pytest.mark.parametrize("key", [".venv", ".grip-review-run.log.alpha", ".grip-review-open.json", ".github", "alpha.grip-review-run.log"])
def test_explicit_migration_uses_shared_key_boundary_without_renaming(legacy_world, monkeypatch, key):
    w = legacy_world
    root, source = w["root"], w["source"]
    row = dict(w["row"], key=key, path=key)
    tree = legacy_bind_tree(source, row)
    old = git(source, "commit-tree", tree, "-m", "legacy key boundary")
    old_ref = legacy_review_ref(old)
    git(source, "update-ref", old_ref, old)
    if source != root:
        # The legal legacy copy is setup, not conversion on the measured read.
        git(root, "fetch", str(source), f"{old_ref}:{old_ref}")
    old_bytes = git(source, "cat-file", "-p", old)
    unsafe = key.startswith(".grip-review") or key == ".venv"
    if unsafe:
        monkeypatch.chdir(root)
        result = CliRunner().invoke(app, ["store", "migrate-reviews", str(root), "--json"])
        assert git(root, "rev-parse", old_ref) == old
        assert git(root, "rev-parse", old + "^{tree}") == tree
        assert git(root, "cat-file", "-p", old) == old_bytes
        assert git(source, "rev-parse", old_ref) == old
        assert git(source, "cat-file", "-p", old) == old_bytes
        # Other valid binds may convert first. Check actual publication for the
        # rejected source, rather than promising an all-binds transaction.
        for ref, oid in refs(root).items():
            if ref == review_ref(oid):
                assert key not in [m["key"] for m in fd.read_record(root, git(root, "rev-parse", oid + "^{tree}"))["members"]]
        for receipt in (root / ".grip" / "receipts").glob("*.json"):
            payload = json.loads(receipt.read_text())
            assert not any(full_id(r["old_id"]) == old and r["status"] in ("created", "present")
                           for r in payload.get("rows", [])), "unsafe source claimed a successful migration"
        assert result.exit_code != 0 and "key" in result.output, f"unsafe legacy key needs named refusal: {result.output}"
    else:
        mappings = migrate_rows(root, monkeypatch)
        mapping = next(r for r in mappings if full_id(r["old_id"]) == old)
        new = full_id(mapping["new_id"])
        record = fd.read_record(root, git(root, "rev-parse", new + "^{tree}"))
        assert record["members"] == [{"key": key, "path": key, "remote": row["remote"], "base": row["base"],
            "commit": row["head"], "remote_head": row["base"], "title": row["title"], "body": row["body"]}]
        assert refs(root)[review_ref(new)] == new and refs(root)[old_ref] == old


@pytest.mark.parametrize("publication", ["writer", "manual-d-control"])
@pytest.mark.parametrize("zero_tests", [False, True], ids=["both-green", "post-test-refusal"])
def test_writer_members_open_run_and_close_with_disjoint_logs(tmp_path, monkeypatch, zero_tests, publication):
    root = native_root(tmp_path / "producer")
    keys = ["alpha", "alpha.grip-review-run.log"]
    rows = []
    for i, key in enumerate(keys):
        work = tmp_path / ("source" + str(i))
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        (work / "README").write_text("base\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "base")
        base = git(work, "rev-parse", "HEAD")
        remote = tmp_path / ("remote" + str(i) + ".git")
        git(tmp_path, "clone", "--bare", str(work), str(remote))
        pkg = "member_pkg" + str(i)
        (work / "src" / pkg).mkdir(parents=True)
        (work / "src" / pkg / "__init__.py").write_text("value = 42\n")
        (work / "tests").mkdir()
        test = "def helper():\n    return 42\n" if zero_tests and i == 1 else f"from {pkg} import value\ndef test_value():\n    assert value == 42\n"
        (work / "tests" / "test_value.py").write_text(test)
        tokens = ["{venv}", "-c", PTH_SCRIPT, key, "{lane}/src", *HOST_PATHS]
        (work / ".review-install").write_text("install = " + " ".join(shlex.quote(t) for t in tokens) + f"\npackage = {pkg}\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "reviewed package")
        rows.append(dict(key=key, path=key, remote=str(remote), base=base, head=git(work, "rev-parse", "HEAD"),
                         ref="refs/heads/main", source=str(work), title=key, body=""))
    if publication == "writer":
        commit = grip.create_review_bind_commit(root, list(reversed(rows)))
    else:
        # Independent adapter control exercises the later lifecycle assertions
        # even while the production writer still emits its old tree.
        members = []
        for row in rows:
            work = Path(row["source"])
            members.append(dict(key=row["key"], path=row["path"], remote=row["remote"],
                base=row["base"], commit=row["head"], remote_head=row["base"], title=row["title"], body="",
                head_tree=git(work, "rev-parse", "HEAD^{tree}"),
                range_patch=(git(work, "format-patch", "--stdout", row["base"] + ".." + row["head"]) + "\n").encode(),
                committers=(git(work, "log", "--reverse", "--format=%cn%x09%ce%x09%cI", row["base"] + ".." + row["head"]) + "\n").encode()))
        tree = fd.write_record(root, dict(schema=grip._REVIEW_BIND_SCHEMA, kind="review", policy="no-policy", members=members))
        fd.verify_tree(root, tree)
        commit = git(root, "commit-tree", tree, "-m", "test-owned form D lifecycle control")
        git(root, "update-ref", review_ref(commit), commit)
    tree = git(root, "rev-parse", commit + "^{tree}")
    fd.verify_tree(root, tree)  # Baseline must fail at producer format, not venv creation.
    assert [m["key"] for m in fd.read_record(root, tree)["members"]] == keys
    lane = tmp_path / "opened"
    monkeypatch.chdir(root)
    opened = CliRunner().invoke(app, ["review", "open", str(root), "gr:" + commit, "--lane-dir", str(lane), "--json"])
    assert opened.exit_code == 0, opened.output
    if zero_tests:
        with pytest.raises(rr.ReviewRunRefused) as exc:
            rr.run_review_lane(lane, pytest_args=["-q"])
        assert exc.value.code == "zero_collected" and exc.value.member == keys[1]
        receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
        assert receipt["output_log"] == ".grip-review-run.log." + keys[1]
    else:
        receipt = rr.run_review_lane(lane, pytest_args=["-q"])
        assert receipt["result"] == "green"
    logs = {".grip-review-run.log." + k: (lane / (".grip-review-run.log." + k)).read_bytes() for k in keys}
    assert all((lane / k).is_dir() for k in keys)
    assert [m["output_log"] for m in receipt["members"]] == [".grip-review-run.log." + k for k in (keys[:1] if zero_tests else keys)]
    closed = close_open_gr_lane(lane, workspace_root=root)
    assert not lane.exists()
    saved = [json.loads(Path(item["receipt"]).read_text()) for item in closed["preserved_runs"]]
    archived = {Path(m["output_log"]).name: Path(m["output_log"]).read_bytes()
                for r in saved for m in [r, *r.get("members", [])] if m.get("output_log")}
    assert archived == logs, "close must carry the actual member logs by receipt name"

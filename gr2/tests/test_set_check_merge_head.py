"""A review whose member heads are MERGE commits (the author resolved a moved base by merging the target) can be
set-checked and merged.

The ordinary multi-agent path: F1 and F2 change the same line; F1 merges; F2's author merges main into F2, resolves,
commits and re-binds against the new base. Carried as `format-patch base..head`, that range linearizes: it emits F2's
original commit (written against the old base) and drops the merge's resolution, so `am` onto the new base fails and
`check set` refused `set_not_reconstructable`. A range that contains a merge is carried as ONE patch, the diff from base
to head, so its reconstruction is the head's TREE (the contract `check set` already accepts as an exact observation).

Rows, and what turns each red:
  R1 a merge-resolved two-member review set-checks and merges, main's second parent the resolved head
                                              -> carry the range with format-patch only
  R2 the carried range for a merge head reconstructs exactly the head's tree, and the review records it as a TREE
     reproduction (never claims the head sha)  -> carry anything other than base..head's diff
  R3 a linear range is carried exactly as before (format-patch, one patch per commit)
                                              -> flatten every range, not only one with a merge
"""
from __future__ import annotations

import json
import subprocess
import sys

from typer.testing import CliRunner

from gr2.python_cli import check_records, grip, merge_gate, set_check
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX
from tests.test_review_merge_gate import _git_identity, git, publish  # noqa: F401  (autouse identity fixture)

runner = CliRunner()
PASS = [sys.executable, "-c", "pass"]


def _world(tmp_path, monkeypatch, *, resolve_by_merge: bool):
    """Two members. On each: F1 (another author) changes payload.txt on main and lands; F2 was cut from the OLD base
    and changes the same line. resolve_by_merge: F2 merges main and resolves (a merge head); else F2 is rebuilt as a
    linear commit on the new main (the control)."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    author = tmp_path / "author"
    author.mkdir()
    out = {"author": author, "members": {}}
    for name in ("alpha", "beta"):
        remote, seed = tmp_path / f"{name}.git", tmp_path / f"seed-{name}"
        git(tmp_path, "init", "--bare", "-b", "main", remote)
        seed.mkdir()
        git(seed, "init", "-b", "main")
        (seed / "payload.txt").write_text("line one\nshared line\nline three\n")
        git(seed, "add", "payload.txt")
        git(seed, "commit", "-m", "base")
        git(seed, "push", remote, "main")
        old_base = git(seed, "rev-parse", "HEAD")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        repo = author / name
        # F2, cut from the old base, before F1 lands.
        git(repo, "checkout", "-b", "feat")
        (repo / "payload.txt").write_text("line one\nshared line, F2\nline three\n")
        git(repo, "add", "payload.txt")
        git(repo, "commit", "-m", "F2")
        # F1 lands on main (another author, same line).
        (seed / "payload.txt").write_text("line one\nshared line, F1\nline three\n")
        git(seed, "add", "payload.txt")
        git(seed, "commit", "-m", "F1")
        git(seed, "push", remote, "main")
        new_base = git(seed, "rev-parse", "HEAD")
        git(repo, "fetch", "origin")
        if resolve_by_merge:
            # the same line on both sides: the merge stops on a conflict (exit 1), the author resolves and commits
            conflict = subprocess.run(["git", "merge", "--no-edit", "origin/main"], cwd=repo, capture_output=True, text=True)
            assert conflict.returncode == 1 and "CONFLICT" in conflict.stdout, (conflict.stdout, conflict.stderr)
            (repo / "payload.txt").write_text("line one\nshared line, F1 and F2\nline three\n")
            git(repo, "add", "payload.txt")
            git(repo, "commit", "--no-edit", "-m", "merge main into F2, resolved")
        else:
            git(repo, "reset", "--hard", "origin/main")
            (repo / "payload.txt").write_text("line one\nshared line, F1 and F2\nline three\n")
            git(repo, "add", "payload.txt")
            git(repo, "commit", "-m", "F2 on the new main")
            (repo / "second.txt").write_text("a second linear commit\n")
            git(repo, "add", "second.txt")
            git(repo, "commit", "-m", "F2 second commit")
        out["members"][name] = {"remote": remote, "base": new_base, "old_base": old_base, "repo": repo,
                                "head": git(repo, "rev-parse", "HEAD")}
    init = runner.invoke(app, ["store", "init", str(author)])
    assert init.exit_code == 0, init.output
    rows = [{"key": n, "path": n, "remote": str(m["remote"]), "base": m["base"], "head": m["head"],
             "ref": "refs/heads/main", "source": str(m["repo"]), "visibility": "public"} for n, m in out["members"].items()]
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "bind", "--rows-json", str(tmp_path / "rows.json")])
    assert made.exit_code == 0, made.output
    out["review"] = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    for m in out["members"].values():
        git(m["repo"], "push", m["remote"], "feat")
        publish(author, out["review"], m["remote"])
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "test", PASS)
    return out


def _is_merge(repo, sha) -> bool:
    return len(git(repo, "rev-list", "--parents", "-n", "1", sha).split()) == 3


def test_R1_a_merge_resolved_review_set_checks_and_merges(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, resolve_by_merge=True)
    assert all(_is_merge(m["repo"], m["head"]) for m in w["members"].values()), "fixture must give merge heads"
    record = set_check.run_set_check(w["author"], w["review"], "set", PASS)
    assert record["result"] == "pass", record
    code, receipt = merge_gate.review_merge(w["author"], w["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt
    for m in w["members"].values():
        tip = git(m["remote"], "rev-parse", "main")
        assert git(m["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [m["base"], m["head"]]


def test_R2_a_merge_range_reconstructs_the_head_tree_as_one_commit(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, resolve_by_merge=True)
    for key, m in w["members"].items():
        lane = tmp_path / f"lane-{key}"
        res = grip.reconstruct_review_lane(w["author"], w["review"].removeprefix("gr:"), key, lane)
        assert git(lane, "rev-parse", "HEAD^{tree}") == git(m["repo"], "rev-parse", "HEAD^{tree}")
        assert res["reconstructed_head"] != res["bound_head"], "a patch cannot rebuild a merge commit's sha"
        assert git(lane, "rev-list", "--count", f"{m['base']}..HEAD") == "1"
    record = set_check.run_set_check(w["author"], w["review"], "set", PASS)
    assert {x["reproduced"] for x in record["members"]} == {"tree"}, record


def test_R3_a_linear_range_is_carried_commit_by_commit_as_before(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, resolve_by_merge=False)
    for key, m in w["members"].items():
        assert not _is_merge(m["repo"], m["head"])
        lane = tmp_path / f"lane-{key}"
        grip.reconstruct_review_lane(w["author"], w["review"].removeprefix("gr:"), key, lane)
        assert git(lane, "rev-list", "--count", f"{m['base']}..HEAD") == "2", "a linear range keeps its commits"
        assert git(lane, "log", "--format=%s", f"{m['base']}..HEAD").splitlines() == ["F2 second commit", "F2 on the new main"]
    record = set_check.run_set_check(w["author"], w["review"], "set", PASS)
    assert record["result"] == "pass", record

"""Bind + verify: the M1.2 gate core, exercised against a local fixture remote.

These two functions (``create_review_bind_commit`` / ``verify_review_commit``)
shipped with no test. This closes that gap network-free: a bare repo advertises
``refs/heads/dev`` at a known base while the reviewed head exists only in a work
clone (a genuinely pre-push head), so the two refusals fire against real
``ls-remote`` output rather than a mock, and verify's NORM sha256 is asserted
against an independently computed hash (the bridge to the hand freeze's NORM).
"""
from __future__ import annotations

from tests.review_ref_helper import legacy_review_ref, review_ref
from tests.native_root_helper import native_root

import hashlib
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import grip
from gr2.python_cli.grip import GripCorruptError, GripReviewRefused

TITLE = "test: prove the bind gate\n"
BODY = "A body with a trailing newline that NORM must strip.\n"


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _fixture_remote(tmp_path: Path) -> tuple[str, str, str]:
    """A bare remote whose ``refs/heads/dev`` is at BASE, plus a HEAD commit that
    exists only in the work clone (never pushed): a pre-push head. Returns
    (remote_url, base_sha, head_sha)."""
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "dev", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-b", "dev", str(work))
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "dev")
    # HEAD: a second commit, deliberately NOT pushed.
    (work / "f.txt").write_text("head\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "head")
    head = _git(work, "rev-parse", "HEAD")
    return str(remote), base, head


def _row(remote: str, base: str, head: str) -> dict[str, str]:
    return {
        "key": "recall", "remote": remote, "path": "recall",
        "head": head, "base": base, "ref": "refs/heads/dev",
        "title": TITLE, "body": BODY,
    }


def _grip_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    native_root(ws)
    return ws


def test_bind_then_verify_reproduces_norm_hashes(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    ws = _grip_ws(tmp_path)

    commit = grip.create_review_bind_commit(ws, [_row(remote, base, head)])
    v = grip.verify_review_commit(ws, commit)

    assert v["tree_matches"] is True
    row = v["rows"][0]
    assert row["head"] == head
    assert row["base"] == base
    assert row["observed_remote_head"] == base
    assert row["base_equals_observed"] == "True"
    # The NORM bridge: verify's sha256 equals an independently computed hash of
    # the trailing-newline-stripped text (the freeze's own NORM rule).
    assert row["title_sha256"] == hashlib.sha256(TITLE.rstrip("\n").encode()).hexdigest()
    assert row["body_sha256"] == hashlib.sha256(BODY.rstrip("\n").encode()).hexdigest()


def test_bind_refuses_when_base_is_not_live_head(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    ws = _grip_ws(tmp_path)
    # base = head is wrong: the live head of dev is BASE, not HEAD.
    with pytest.raises(GripReviewRefused) as exc:
        grip.create_review_bind_commit(ws, [_row(remote, head, head)])
    assert exc.value.refusal == "base_not_live_head"


def test_bind_refuses_when_head_already_on_remote(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    ws = _grip_ws(tmp_path)
    # Publish the head under some ref so it is present on the remote.
    work2 = tmp_path / "work2"
    _git(tmp_path, "clone", remote, str(work2))
    _git(work2, "fetch", str(tmp_path / "work"), f"{head}:refs/heads/pushed")
    _git(work2, "push", "origin", f"{head}:refs/heads/pushed")

    with pytest.raises(GripReviewRefused) as exc:
        grip.create_review_bind_commit(ws, [_row(remote, base, head)])
    assert exc.value.refusal == "head_already_on_remote"

    # A named ratify receipt is the sanctioned fix-forward (a bare override is not).
    commit = grip.create_review_bind_commit(ws, [_row(remote, base, head)], ratified="rcpt-1")
    assert grip.verify_review_commit(ws, commit)["tree_matches"] is True


def test_bind_carries_frozen_set_and_verify_reproduces_objects(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"  # the author's clone, holds the pre-push head
    ws = _grip_ws(tmp_path)

    row = _row(remote, base, head)
    row["source"] = str(work)
    row["evidence"] = "label: T\ncommand: true\nexit: 0\n"
    commit = grip.create_review_bind_commit(ws, [row])

    v = grip.verify_review_commit(ws, commit)
    assert v["tree_matches"] is True
    r = v["rows"][0]
    # head-tree is recorded so run can assert reconstruction WITHOUT the head object.
    assert r["head_tree"] == _git(work, "rev-parse", f"{head}^{{tree}}")
    assert len(r["range_sha256"]) == 64
    # Decode by field number, so this checks content through the D reader.
    from gr2.python_cli import review_field_tree as fd
    tree = _git(ws, "rev-parse", commit + "^{tree}")
    member = fd.read_record(ws, tree)["members"][0]
    assert member["head_tree"] == _git(work, "rev-parse", head + "^{tree}")
    assert b"Subject:" in member["range_patch"] and b"diff --git" in member["range_patch"]
    assert member["metadata"]
    assert member["evidence"] == {"commands": row["evidence"].encode()}


def test_reconstruct_materializes_pre_push_head_and_asserts_tree(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"
    ws = _grip_ws(tmp_path)
    row = _row(remote, base, head)
    row["source"] = str(work)
    commit = grip.create_review_bind_commit(ws, [row])

    # The head is genuinely pre-push (only on the bare remote's dev is BASE).
    out = grip.reconstruct_review_lane(ws, commit, "recall", tmp_path / "lane")
    # Reconstructed tree equals the bound head-tree AND the author's real head tree,
    # without the head object ever being on the remote.
    assert out["reconstructed_tree"] == out["bound_head_tree"]
    assert out["reconstructed_tree"] == _git(work, "rev-parse", f"{head}^{{tree}}")
    # The reconstructed HEAD sha legitimately differs from the author's bound head:
    # reconstruction reproduces the TREE, not the committer identity. The design
    # records both SHAs side by side; equality of the TREE is the assertion.
    assert len(out["reconstructed_head"]) == 40
    assert out["bound_head"] == head


def test_reconstruct_refuses_on_tree_mismatch(tmp_path):
    """The tree assertion is the load-bearing guard: a carried head-tree that the
    range does not reproduce (corruption or a swapped object) REFUSES before any
    check runs. Built by tampering the stored head-tree to a wrong-but-valid sha."""
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"
    ws = _grip_ws(tmp_path)
    row = _row(remote, base, head)
    row["source"] = str(work)
    commit = grip.create_review_bind_commit(ws, [row])

    from gr2.python_cli import review_field_tree as fd
    wrong_tree = _git(work, "rev-parse", f"{base}^{{tree}}")  # base tree != head tree (a real, wrong sha)
    # Replace the member's head_tree field (Member field 6) in the field tree, then commit it.
    tree = _git(ws, "rev-parse", f"{commit}^{{tree}}")
    bad = fd.replace_entry(ws, tree, ["004.2", "0000001", "006.2"], wrong_tree.encode())
    tampered = subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e",
         "commit-tree", bad, "-m", "tampered"],
        capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(ws), "update-ref", review_ref(tampered), tampered], check=True)

    with pytest.raises(GripReviewRefused) as exc:
        grip.reconstruct_review_lane(ws, tampered, "recall", tmp_path / "lane2")
    assert exc.value.refusal == "tree_mismatch"


def test_run_executes_declared_checks_in_the_lane(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"
    ws = _grip_ws(tmp_path)
    row = _row(remote, base, head)
    row["source"] = str(work)
    row["evidence"] = (
        "label: HEAD_SHA\ncommand: git rev-parse HEAD\nexit: 0\n"
        "---\n"
        "label: TREE\ncommand: git rev-parse HEAD^{tree}\nexit: 0\n"
    )
    commit = grip.create_review_bind_commit(ws, [row])

    result = grip.run_review_checks(ws, commit, "recall", tmp_path / "lane")
    runs = result["runs"]
    assert [r["label"] for r in runs] == ["HEAD_SHA", "TREE"]
    assert all(r["exit"] == 0 and r["exit_matched"] for r in runs)
    # Each check ran with cwd = the reconstructed lane, not the base workspace.
    assert all(r["cwd"] == result["materialized"]["lane"] for r in runs)
    # Resolution pins imports at the lane (no src/ in this fixture, so the lane root).
    assert result["import_resolution"] == result["materialized"]["lane"]


def test_receipt_binds_axes_actor_liveness_and_gates_blocking_findings(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"
    ws = _grip_ws(tmp_path)
    row = _row(remote, base, head)
    row["source"] = str(work)
    row["evidence"] = "label: T\ncommand: true\nexit: 0\n"
    commit = grip.create_review_bind_commit(ws, [row])
    run_results = grip.run_review_checks(ws, commit, "recall", tmp_path / "lane")

    receipt = grip.build_review_receipt(
        ws, commit, actor="apollo", verdict="ratify",
        axes={"code": "ran", "disclosure": "read"},
        run_results=run_results, read=["texts/recall/title", "texts/recall/body"],
    )
    assert receipt["actor"] == "apollo"
    assert receipt["axes"] == {"code": "ran", "disclosure": "read"}
    assert receipt["verify"]["tree_matches"] is True
    assert receipt["materialized"]["reconstructed_tree"] == receipt["materialized"]["bound_head_tree"]
    # Liveness: the bound base is still the live head of dev on the fixture remote.
    assert receipt["liveness"][0]["state"] == "equal"
    assert receipt["expires_on"]["any_base_moved"] is False

    # A block needs a complete blocking finding.
    with pytest.raises(grip.GripReviewRefused) as e1:
        grip.build_review_receipt(ws, commit, actor="a", verdict="block",
                                  axes={}, run_results=run_results, findings=[])
    assert e1.value.refusal == "block_without_blocking_finding"
    with pytest.raises(grip.GripReviewRefused) as e2:
        grip.build_review_receipt(ws, commit, actor="a", verdict="block",
                                  axes={}, run_results=run_results,
                                  findings=[{"blocking": True, "claim": "x", "seam": "s"}])
    assert e2.value.refusal == "incomplete_blocking_finding"


def test_policy_hook_sees_carried_bytes_and_refuses_on_hit(tmp_path):
    remote, base, head = _fixture_remote(tmp_path)
    work = tmp_path / "work"
    ws = _grip_ws(tmp_path)
    # A hook that reads every file in the passed dir and fails if it finds SECRET.
    hook = ["python3", "-c",
            "import sys,glob,os;d=sys.argv[1];"
            "sys.exit(1 if any('SECRET' in open(f).read() for f in glob.glob(os.path.join(d,'*'))) else 0)"]

    # Clean texts: the hook clears and its verdict is recorded in the object.
    clean = _row(remote, base, head)
    clean["source"] = str(work)
    commit = grip.create_review_bind_commit(ws, [clean], policy_hook=hook)
    from gr2.python_cli import review_field_tree as fd
    policy = fd.read_record(ws, _git(ws, "rev-parse", f"{commit}^{{tree}}"))["policy"]
    assert policy.startswith("clean:")
    assert grip.verify_review_commit(ws, commit)["tree_matches"] is True

    # A carried leak (in the body text) refuses the bind, like a freeze does.
    leaky = _row(remote, base, head)
    leaky["source"] = str(work)
    leaky["body"] = "this body contains a SECRET that must not reach a public remote\n"
    with pytest.raises(grip.GripReviewRefused) as exc:
        grip.create_review_bind_commit(ws, [leaky], policy_hook=hook)
    assert exc.value.refusal == "policy_hook_refused"


def test_verify_refuses_a_non_bind_commit(tmp_path):
    ws = _grip_ws(tmp_path)
    # A plain project-review commit is not a review-bind commit.
    plain = grip.create_project_review_commit(
        ws, [{"key": "recall", "repo": "https://github.com/o/r", "path": "recall",
              "base": "0" * 40, "head": "1" * 40}]
    )
    with pytest.raises(GripCorruptError):
        grip.verify_review_commit(ws, plain)

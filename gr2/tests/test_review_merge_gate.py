"""`review_merge` merges a bound review into a plain bare remote, or refuses before anything moves.

Smallest working proof for the plain-remote merge gate: one member, a bare remote, a real bind, a real
exact-head check record, and the gate's verdict read back from the remote.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, merge_gate
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX

runner = CliRunner()


def git(root, *args):
    p = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    remote, seed, author = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "author"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "payload.txt").write_text("base\n")
    git(seed, "add", "payload.txt")
    git(seed, "commit", "-m", "base")
    base = git(seed, "rev-parse", "HEAD")
    git(seed, "push", remote, "main")
    author.mkdir()
    git(author, "clone", "--no-local", "--branch", "main", remote, "member")
    init = runner.invoke(app, ["store", "init", str(author)])
    assert init.exit_code == 0, init.output
    member = author / "member"
    git(member, "checkout", "-b", "feat")
    (member / "payload.txt").write_text("reviewed\n")
    git(member, "add", "payload.txt")
    git(member, "commit", "-m", "reviewed")
    head = git(member, "rev-parse", "HEAD")
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "bind", "--repo", "member", "--remote", str(remote), "--base", base,
                               "--head", head, "--ref", "refs/heads/main", "--source", str(member)])
    assert made.exit_code == 0, made.output
    review = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    git(member, "push", remote, "feat")
    return dict(author=author, member=member, remote=remote, base=base, head=head, review=review)


def check(w):
    check_records.run_check(w["member"], str(w["remote"]), w["head"], "test", [sys.executable, "-c", "pass"])


def merge(w):
    return merge_gate.review_merge(w["author"], w["review"], into="main", feature="feat")


def test_merges_the_reviewed_head_and_a_rerun_is_idempotent(world):
    check(world)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt
    assert receipt["members"][0]["state"] == "already_merged"
    assert git(world["remote"], "rev-parse", "main") == tip, "a rerun must not merge twice"


def test_refuses_a_head_moved_after_review(world):
    check(world)
    (world["member"] / "moved.txt").write_text("after review\n")
    git(world["member"], "add", "moved.txt")
    git(world["member"], "commit", "-m", "moved after review")
    git(world["member"], "push", world["remote"], "feat")
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("feature_moved"), receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_refuses_without_a_check_record(world):
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("check_absent"), receipt
    assert git(world["remote"], "rev-parse", "main") == world["base"]


def test_refuses_a_moved_base(world, tmp_path):
    check(world)
    other = tmp_path / "other"
    git(tmp_path, "clone", "--no-local", "--branch", "main", world["remote"], other)
    (other / "unrelated.txt").write_text("someone else\n")
    git(other, "add", "unrelated.txt")
    git(other, "commit", "-m", "unrelated")
    git(other, "push", "origin", "main")
    moved = git(world["remote"], "rev-parse", "main")
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("base_moved"), receipt
    assert git(world["remote"], "rev-parse", "main") == moved

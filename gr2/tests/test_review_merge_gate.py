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


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """gr2 writes commits (store, bind, check records) with the ambient identity; a clean box has none."""
    for k, v in (("GIT_AUTHOR_NAME", "Fixture"), ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
                 ("GIT_COMMITTER_NAME", "Fixture"), ("GIT_COMMITTER_EMAIL", "fixture@example.invalid")):
        monkeypatch.setenv(k, v)


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
    world = dict(author=author, member=member, remote=remote, base=base, head=head, review=review)
    publish(world["author"], review, remote)
    return world


def publish(author, review, remote):
    out = runner.invoke(app, ["review", "publish", review, "--remote", str(remote)])
    assert out.exit_code == 0, out.output


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


@pytest.fixture
def slice2(tmp_path, monkeypatch):
    """Two members, alpha and beta, bound into ONE review with --rows-json."""
    import json
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
        (seed / "payload.txt").write_text(f"{name} base\n")
        git(seed, "add", "payload.txt")
        git(seed, "commit", "-m", "base")
        git(seed, "push", remote, "main")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        out["members"][name] = {"remote": remote, "base": git(seed, "rev-parse", "HEAD"), "repo": author / name}
    init = runner.invoke(app, ["store", "init", str(author)])
    assert init.exit_code == 0, init.output
    rows = []
    for name, m in out["members"].items():
        git(m["repo"], "checkout", "-b", "feat")
        (m["repo"] / "payload.txt").write_text(f"{name} reviewed\n")
        git(m["repo"], "add", "payload.txt")
        git(m["repo"], "commit", "-m", "reviewed")
        m["head"] = git(m["repo"], "rev-parse", "HEAD")
        rows.append({"key": name, "path": name, "remote": str(m["remote"]), "base": m["base"], "head": m["head"],
                     "ref": "refs/heads/main", "source": str(m["repo"])})
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "bind", "--rows-json", str(tmp_path / "rows.json")])
    assert made.exit_code == 0, made.output
    out["review"] = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    for m in out["members"].values():
        git(m["repo"], "push", m["remote"], "feat")
        publish(author, out["review"], m["remote"])
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "test", [sys.executable, "-c", "pass"])
    return out


def move(m, tmp_path_name="moved.txt"):
    (m["repo"] / tmp_path_name).write_text("after review\n")
    git(m["repo"], "add", tmp_path_name)
    git(m["repo"], "commit", "-m", "moved after review")
    git(m["repo"], "push", m["remote"], "feat")


def mains(s):
    return {k: git(m["remote"], "rev-parse", "main") for k, m in s["members"].items()}


def test_a_two_member_slice_merges_as_a_set(slice2):
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED, receipt
    for k, m in slice2["members"].items():
        parents = git(m["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:]
        assert parents == [m["base"], m["head"]], (k, parents)


def test_moving_the_last_member_refuses_the_whole_set(slice2):
    move(slice2["members"]["beta"])
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == {k: m["base"] for k, m in slice2["members"].items()}, "no member may merge"


def test_a_rerun_after_an_earlier_partial_reports_partial_not_refused(slice2):
    # alpha merged by an earlier run; then beta moves; the rerun refuses beta but the STATE is partial.
    code, _ = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED
    beta = slice2["members"]["beta"]
    git(beta["remote"], "update-ref", "refs/heads/main", beta["base"])  # undo beta's merge on the remote
    move(beta)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_PARTIAL, receipt


def test_refuses_a_review_not_published_on_the_member_remote(world):
    check(world)
    ref = f"{REVIEW_REF_PREFIX}{world['review'].removeprefix('gr:')}"
    assert git(world["remote"], "rev-parse", "--verify", ref), "the published review must exist before it is removed"
    git(world["remote"], "update-ref", "-d", ref)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("review_not_on_member_remote"), receipt


def test_a_nested_ref_with_the_same_tail_does_not_count_as_the_published_review(world):
    check(world)
    ref = f"{REVIEW_REF_PREFIX}{world['review'].removeprefix('gr:')}"
    rid = git(world["remote"], "rev-parse", ref)
    git(world["remote"], "update-ref", f"refs/decoy/{ref}", rid)  # same tail, different (nested) ref
    git(world["remote"], "update-ref", "-d", ref)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("review_not_on_member_remote"), receipt


# Each refusal names its next command, and running that command as printed gets past the refusal.
FORBIDDEN_ADVICE = ("git config", "git_config", "user.name", "user.email", "delete", "push -d",
                    "git_author_", "git_committer_")


def next_argv(line, fill=()):
    import shlex
    assert line.startswith("next: gr2 "), line
    argv = shlex.split(line.removeprefix("next: gr2 "))
    if "--" in argv and argv[-1].endswith("command>"):
        argv = argv[:argv.index("--") + 1] + list(fill)
    return argv


def test_an_unpublished_review_names_publish_and_the_printed_command_gets_past_it(world):
    check(world)
    git(world["remote"], "update-ref", "-d", f"{REVIEW_REF_PREFIX}{world['review'].removeprefix('gr:')}")
    code, receipt = merge(world)
    row = receipt["members"][0]
    assert code == merge_gate.EXIT_REFUSED and row["refused"].startswith("review_not_on_member_remote"), receipt
    ran = runner.invoke(app, next_argv(row["next"]))
    assert ran.exit_code == 0, ran.output
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_missing_check_names_check_run_and_the_printed_command_gets_past_it(world):
    code, receipt = merge(world)
    row = receipt["members"][0]
    assert code == merge_gate.EXIT_REFUSED and row["refused"].startswith("check_absent"), receipt
    ran = runner.invoke(app, next_argv(row["next"], [sys.executable, "-c", "pass"]))
    assert ran.exit_code == 0, ran.output
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_pinning_a_head_already_pushed_names_the_non_destructive_path(world):
    again = runner.invoke(app, ["review", "pin", "--repo", "member", "--remote", str(world["remote"]),
                                "--base", world["base"], "--head", world["head"], "--ref", "refs/heads/main",
                                "--source", str(world["member"])])
    assert again.exit_code == 2 and "head_already_on_remote" in again.output, again.output
    line = next(x for x in again.output.splitlines() if x.startswith("next: "))
    assert "--ratified" in line and not any(f in line.lower() for f in FORBIDDEN_ADVICE), line


def test_no_next_line_and_no_help_line_suggests_an_identity_or_delete_trick():
    from gr2.python_cli import next_steps
    lines = [next_steps.self_approval("/ws", "gr:abc"), next_steps.head_already_on_remote(), next_steps.REVIEW_ORDER]
    for refused in ("review_not_on_member_remote: x", "check_absent: x", "check_fail: x", "feature_moved: x",
                    "base_moved: x", "approvals_insufficient: 0 of 1"):
        line = next_steps.merge_row(refused, workspace="/ws", review="gr:abc", remote="/r.git", path="/ws/m",
                                    head="a" * 40, checks=("test",))
        assert line, refused
        lines.append(line)
    for line in lines:
        assert not any(f in line.lower() for f in FORBIDDEN_ADVICE), line


def test_review_help_gives_the_order_of_the_steps():
    out = runner.invoke(app, ["review", "--help"])
    assert out.exit_code == 0, out.output
    text = " ".join(out.output.split())
    order = [text.index(f"gr2 review {v}") for v in ("pin", "stamp", "publish", "merge")]
    assert order == sorted(order) and "gr2 check run" in text, out.output


def test_two_required_checks_get_one_check_run_line_each_and_both_lines_get_past_the_refusal(world):
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_checks=("lint", "test"))
    row = receipt["members"][0]
    assert code == merge_gate.EXIT_REFUSED and row["refused"].startswith("check_absent"), receipt
    lines = row["next"].splitlines()
    assert len(lines) == 2 and all(x.count("--name") <= 1 for x in lines), lines
    assert sum("--name lint" in x for x in lines) == 1, lines
    for line in lines:
        ran = runner.invoke(app, next_argv(line, [sys.executable, "-c", "pass"]))
        assert ran.exit_code == 0, ran.output
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_checks=("lint", "test"))
    assert code == merge_gate.EXIT_MERGED, receipt


@pytest.mark.parametrize("use_rich", ["1", "0"])
def test_review_help_keeps_each_step_on_its_own_line(use_rich):
    import os
    env = dict(os.environ, TYPER_USE_RICH=use_rich, COLUMNS="100")
    out = subprocess.run([sys.executable, "-m", "gr2.python_cli.app", "review", "--help"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    starts = [x.strip(" │").split("  ")[0] for x in out.stdout.splitlines()]
    for n, step in enumerate(("gr2 review pin", "push the head", "gr2 review stamp", "gr2 check run",
                              "gr2 review publish", "gr2 review merge"), 1):
        assert f"{n}. {step}" in starts, (use_rich, out.stdout)

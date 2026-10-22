"""`review_merge` merges a bound review into a plain bare remote, or refuses before anything moves.

Smallest working proof for the plain-remote merge gate: one member, a bare remote, a real bind, a real
exact-head check record, and the gate's verdict read back from the remote.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, merge_gate, set_check
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
    return merge_gate.review_merge(w["author"], w["review"], into="main", feature="feat", required_approvals=0)


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


def test_a_replace_ref_on_the_head_does_not_turn_a_clean_merge_into_a_conflict(world):
    # A replace ref swapping the head for a parentless commit leaves merge-tree no merge base, so a clean
    # merge would be refused merge_conflict; the build reads the real history.
    check(world)
    m = world["member"]
    root = git(m, "commit-tree", world["head"] + "^{tree}", "-m", "parentless")
    git(m, "replace", world["head"], root)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]


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
    return make_slice(tmp_path, monkeypatch, ("alpha", "beta"))


def make_slice(tmp_path, monkeypatch, names):
    import json
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    author = tmp_path / "author"
    author.mkdir()
    out = {"author": author, "members": {}}
    for name in names:
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
                     "ref": "refs/heads/main", "source": str(m["repo"]), "visibility": "public"})
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


def record_set(s):
    """A passing `set` record at the pinned heads: what a review over several members merges on."""
    rec = set_check.run_set_check(s["author"], s["review"], "set", [sys.executable, "-c", "pass"])
    assert rec.get("result") == "pass", rec


def move(m, tmp_path_name="moved.txt"):
    (m["repo"] / tmp_path_name).write_text("after review\n")
    git(m["repo"], "add", tmp_path_name)
    git(m["repo"], "commit", "-m", "moved after review")
    git(m["repo"], "push", m["remote"], "feat")


def mains(s):
    return {k: git(m["remote"], "rev-parse", "main") for k, m in s["members"].items()}


def test_a_two_member_slice_merges_as_a_set(slice2):
    record_set(slice2)  # a review over several members merges only on a passing set
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt
    for k, m in slice2["members"].items():
        parents = git(m["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:]
        assert parents == [m["base"], m["head"]], (k, parents)


def test_moving_the_last_member_refuses_the_whole_set(slice2):
    record_set(slice2)  # a review over several members merges only on a passing set
    move(slice2["members"]["beta"])
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == {k: m["base"] for k, m in slice2["members"].items()}, "no member may merge"


def test_a_rerun_after_an_earlier_partial_reports_partial_not_refused(slice2):
    record_set(slice2)  # a review over several members merges only on a passing set
    # alpha merged by an earlier run; then beta moves; the rerun refuses beta but the STATE is partial.
    code, _ = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED
    beta = slice2["members"]["beta"]
    git(beta["remote"], "update-ref", "refs/heads/main", beta["base"])  # undo beta's merge on the remote
    move(beta)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", required_approvals=0)
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
    # Now a head that IS the pushed branch tip pins (test_review_pin_after_push); the
    # refusal that remains is a branch that moved past the head after the push.
    (world["member"] / "later.txt").write_text("later\n")
    git(world["member"], "add", "later.txt")
    git(world["member"], "commit", "-m", "later")
    tip = git(world["member"], "rev-parse", "HEAD")
    git(world["member"], "push", str(world["remote"]), "feat")
    again = runner.invoke(app, ["review", "pin", "--repo", "member", "--remote", str(world["remote"]),
                                "--base", world["base"], "--head", world["head"], "--ref", "refs/heads/main",
                                "--source", str(world["member"])])
    assert again.exit_code == 2 and "head_behind_remote_branch" in again.output, again.output
    line = next(x for x in again.output.splitlines() if x.startswith("next: "))
    assert f"--head {tip}" in line and not any(f in line.lower() for f in FORBIDDEN_ADVICE), line


def test_no_next_line_and_no_help_line_suggests_an_identity_or_delete_trick():
    from gr2.python_cli import next_steps
    lines = [next_steps.self_approval("/ws", "gr:abc"), next_steps.head_already_on_remote(), next_steps.REVIEW_ORDER,
             next_steps.not_received("/ws", "gr:abc"),
             next_steps.head_not_fetched("/ws/m", "/r.git", "a" * 40, "/ws", "gr:abc")]
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
    # publish comes before stamp: a reviewer in another workspace can only receive a published review
    order = [text.index(f"gr2 review {v}") for v in ("pin", "publish", "stamp", "merge")]
    assert order == sorted(order) and "gr2 check run" in text, out.output


def test_two_required_checks_get_one_check_run_line_each_and_both_lines_get_past_the_refusal(world):
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_checks=("lint", "test"), required_approvals=0)
    row = receipt["members"][0]
    assert code == merge_gate.EXIT_REFUSED and row["refused"].startswith("check_absent"), receipt
    lines = row["next"].splitlines()
    assert len(lines) == 2 and all(x.count("--name") <= 1 for x in lines), lines
    assert sum("--name lint" in x for x in lines) == 1, lines
    for line in lines:
        ran = runner.invoke(app, next_argv(line, [sys.executable, "-c", "pass"]))
        assert ran.exit_code == 0, ran.output
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_checks=("lint", "test"), required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt


@pytest.mark.parametrize("use_rich", ["1", "0"])
def test_review_help_keeps_each_step_on_its_own_line(use_rich):
    import os
    env = dict(os.environ, TYPER_USE_RICH=use_rich, COLUMNS="100")
    out = subprocess.run([sys.executable, "-m", "gr2.python_cli.app", "review", "--help"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)  # CI forces colour; the layout is what is under test
    starts = [x.strip(" │").split("  ")[0] for x in plain.splitlines()]
    for n, step in enumerate(("gr2 review pin", "gr2 review publish", "gr2 review stamp",
                              "gr2 check run", "gr2 review merge"), 1):
        assert f"{n}. {step}" in starts, (use_rich, out.stdout)


def test_a_printed_next_line_escapes_control_characters_and_quotes_each_value():
    from gr2.python_cli import next_steps
    import shlex
    hostile = "/ws/m\x1b[31mRED\nnext: gr2 evil\x9b\u202emoc.live\u2028x\u200b"
    lines = [next_steps.merge_row("check_absent: x", workspace="/ws", review="gr:abc", remote="/r.git", path=hostile,
                                  head="a" * 40, checks=("test",)),
             next_steps.merge_row("review_not_on_member_remote: x", workspace="/my ws", review="gr:abc",
                                  remote=hostile, path="/ws/m", head="a" * 40, checks=("test",))]
    for line in lines:
        assert all(c.isprintable() for c in line), repr(line)
        assert len(line.splitlines()) == 1 and line.startswith("next: "), line  # no forged second line
    assert "/my ws" in shlex.split(lines[1]), lines[1]


def test_a_push_refused_mid_run_is_partial_and_a_rerun_after_the_cause_clears_finishes_it(slice2, tmp_path):
    # The README's partial-merge paragraph: separate pushes, so one member can land while another is refused; the
    # exit is 4, `review merged` reads the same state, and a rerun skips the landed member and pushes the rest.
    record_set(slice2)
    beta = slice2["members"]["beta"]
    hook = beta["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'main is closed for now' >&2\nexit 1\n")
    hook.chmod(0o755)
    argv = ["review", "merge", slice2["review"], "--from", "feat", "--into", "main", "--approvals", "0"]
    first = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\n{first.output}exit {first.exit_code}")
    assert first.exit_code == merge_gate.EXIT_PARTIAL, first.output
    landed = mains(slice2)
    assert landed["beta"] == beta["base"], "the refused member's target did not move"
    assert landed["alpha"] != slice2["members"]["alpha"]["base"], "the earlier member landed"
    receipt = {m["key"]: m for m in json.loads(next(l for l in first.output.splitlines() if l.startswith("{")))["members"]}
    assert receipt["alpha"]["state"] == "merged" and not receipt["alpha"].get("push_error"), receipt["alpha"]
    assert receipt["beta"]["state"] == "unmerged" and receipt["beta"]["push_error"], receipt["beta"]
    state = runner.invoke(app, ["review", "merged", slice2["review"], "--into", "main"])
    print(f"$ gr2 review merged {slice2['review']} --into main\n{state.output}exit {state.exit_code}")
    assert state.exit_code == merge_gate.EXIT_PARTIAL, state.output
    read = {m["key"]: m["merged"] for m in json.loads(next(l for l in state.output.splitlines() if l.startswith("{")))["members"]}
    assert read == {"alpha": landed["alpha"], "beta": None}, read
    hook.unlink()  # the cause clears
    again = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\n{again.output}exit {again.exit_code}")
    assert again.exit_code == merge_gate.EXIT_MERGED, again.output
    rerun = {m["key"]: m["state"] for m in json.loads(next(l for l in again.output.splitlines() if l.startswith("{")))["members"]}
    assert rerun == {"alpha": "already_merged", "beta": "merged"}, rerun
    assert mains(slice2)["alpha"] == landed["alpha"], "the rerun did not merge the landed member twice"
    parents = git(beta["remote"], "rev-list", "--parents", "-n", "1", "main").split()[1:]
    assert parents == [beta["base"], beta["head"]], parents


def test_a_partial_merge_names_what_merged_and_what_did_not_on_stderr(slice2):
    # Exit 4: one stderr line names each landed member with its sha and each other member with its reason code;
    # stdout stays the one JSON receipt.
    record_set(slice2)
    hook = slice2["members"]["beta"]["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'main is closed for now' >&2\nexit 1\n")
    hook.chmod(0o755)
    argv = ["review", "merge", slice2["review"], "--from", "feat", "--into", "main", "--approvals", "0"]
    out = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\nstdout: {out.stdout}stderr: {out.stderr}exit {out.exit_code}")
    assert out.exit_code == merge_gate.EXIT_PARTIAL, out.output
    assert len(out.stdout.splitlines()) == 1, out.stdout
    receipt = json.loads(out.stdout)
    assert receipt["exit"] == merge_gate.EXIT_PARTIAL, receipt
    sha = mains(slice2)["alpha"][:12]
    lines = [l for l in out.stderr.splitlines() if l.startswith("partial:")]
    assert lines == [f"partial: merged alpha ({sha}); not merged beta (push_rejected)"], out.stderr


def test_a_full_merge_prints_no_partial_line(slice2):
    record_set(slice2)
    argv = ["review", "merge", slice2["review"], "--from", "feat", "--into", "main", "--approvals", "0"]
    out = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\nstdout: {out.stdout}stderr: {out.stderr}exit {out.exit_code}")
    assert out.exit_code == merge_gate.EXIT_MERGED, out.output
    assert len(out.stdout.splitlines()) == 1, out.stdout
    assert json.loads(out.stdout)["exit"] == merge_gate.EXIT_MERGED, out.stdout
    assert "partial:" not in out.stderr, out.stderr


def test_the_readme_names_the_partial_line_the_helper_prints():
    # The code never says more than the README: the sentence under the exit codes names the line's shape, and the
    # helper's line starts with that shape's literal prefix.
    from pathlib import Path

    from gr2.python_cli import next_steps
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    shape = "`partial: merged <key> (<sha12>); not merged <key> (<reason>); not known <key> (<reason>)`"
    assert shape in readme, "README names the exit-4 stderr line"
    assert "`partial: nothing measured (<code>)`" in readme, "README names the no-members line"
    assert "shows it beside the sha as `<key> (<sha12>; <code>)`" in readme, "README names the merged-with-code entry"
    prefix = shape.strip("`").split("<", 1)[0]
    receipt = {"members": [{"key": "alpha", "final": "merged", "merged": "a" * 40},
                           {"key": "beta", "final": "unmerged", "refused": "push_rejected: main is closed"},
                           {"key": "gamma", "final": "unknown", "refused": None}]}
    line = next_steps._merge_partial_line(merge_gate.EXIT_PARTIAL, receipt)
    assert prefix == "partial: merged " and line.startswith(prefix), line
    assert line == f"partial: merged alpha ({'a' * 12}); not merged beta (push_rejected); not known gamma (unknown)", line


@pytest.mark.parametrize("code", ["platform_head_moved_after_merge", "platform_head_unmeasured_after_merge"])
def test_a_merged_member_with_a_code_shows_the_code_beside_the_sha(code):
    # Exit 4 with every member merged: the stranded or unmeasured member's code is the one thing the line can name.
    from gr2.python_cli import next_steps
    receipt = {"members": [{"key": "alpha", "final": "merged", "merged": "a" * 40, "refused": f"{code}: PR #7 detail"},
                           {"key": "beta", "final": "merged", "merged": "a" * 40, "refused": None}]}
    line = next_steps._merge_partial_line(merge_gate.EXIT_PARTIAL, receipt)
    assert line == f"partial: merged alpha ({'a' * 12}; {code}), beta ({'a' * 12})", line


def test_a_bind_fault_partial_line_says_nothing_was_measured():
    # merge_gate._unmeasured: exit 4 with no members; the line names the receipt's refusal code, not "merged none".
    from gr2.python_cli import next_steps
    code, receipt = merge_gate._unmeasured("r1", "main", "feat", "bind_unreadable: OSError: gone")
    assert code == merge_gate.EXIT_PARTIAL and receipt["members"] == [], receipt
    assert next_steps._merge_partial_line(code, receipt) == "partial: nothing measured (bind_unreadable)"


@pytest.mark.parametrize("receipt", [
    pytest.param({"members": [{"key": "alpha", "final": "merged", "merged": "ab\npartial: "}]}, id="sha-newline"),
    pytest.param({"members": [{"key": "alpha", "final": "merged", "merged": "\x1b[2K\rforged"}]}, id="sha-escape"),
    pytest.param({"members": [], "refused": "x\npartial"}, id="no-members-code-newline"),
    pytest.param({"members": [], "refused": 7}, id="no-members-code-int"),
    pytest.param({"members": [{"key": "alpha", "final": "merged", "merged": "a" * 40, "refused": 7}]}, id="member-code-int"),
    pytest.param({"members": [{"key": "alpha", "final": "merged", "merged": "a" * 40, "refused": "x\npartial"}]},
                 id="merged-code-newline"),
    pytest.param({"members": [{"key": "alpha", "final": "unmerged", "refused": "x\npartial"}]}, id="unmerged-code-newline"),
    pytest.param({"members": [{"key": "alpha", "final": "unknown\x1b[2K\rforged"}]}, id="final-escape"),
])
def test_no_receipt_field_forges_a_line(receipt):
    # Every field the partial line prints goes through _shown: one line, no terminal escape, no raw control byte.
    from gr2.python_cli import next_steps
    line = next_steps._merge_partial_line(merge_gate.EXIT_PARTIAL, receipt)
    assert line.startswith("partial: ") and len(line.splitlines()) == 1, line
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in line), repr(line)


def test_an_unconfirmed_push_is_not_known_not_unmerged():
    # A push that timed out may still land: the member is "not known", even when the target then read unmerged.
    from gr2.python_cli import next_steps
    receipt = {"members": [{"key": "alpha", "final": "merged", "merged": "b" * 40},
                           {"key": "beta", "final": "unmerged", "refused": "push_unconfirmed: timed out"}]}
    line = next_steps._merge_partial_line(merge_gate.EXIT_PARTIAL, receipt)
    assert line == f"partial: merged alpha ({'b' * 12}); not known beta (push_unconfirmed)", line


def test_a_member_never_measured_is_not_known():
    # final == "unknown" (a member the run never selected, or a measurement fault): never "not merged".
    from gr2.python_cli import next_steps
    receipt = {"members": [{"key": "alpha", "final": "merged", "merged": "c" * 40},
                           {"key": "beta", "final": "unknown", "refused": None}]}
    line = next_steps._merge_partial_line(merge_gate.EXIT_PARTIAL, receipt)
    assert line == f"partial: merged alpha ({'c' * 12}); not known beta (unknown)", line


def test_a_member_that_moves_after_a_half_land_is_refused_with_a_next_line_and_the_run_stays_partial(slice2):
    # The README's half-land paragraph: a member whose branch moved before the rerun is refused by the same preflight,
    # the refusal carries a `next:` step in the receipt, its target does not move, and the run still exits 4.
    record_set(slice2)
    beta = slice2["members"]["beta"]
    hook = beta["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'main is closed for now' >&2\nexit 1\n")
    hook.chmod(0o755)
    argv = ["review", "merge", slice2["review"], "--from", "feat", "--into", "main", "--approvals", "0"]
    first = runner.invoke(app, argv)
    assert first.exit_code == merge_gate.EXIT_PARTIAL, first.output
    hook.unlink()  # the cause clears, but beta's branch moved meanwhile
    move(beta)
    again = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\n{again.output}exit {again.exit_code}")
    assert again.exit_code == merge_gate.EXIT_PARTIAL, again.output
    rows = {m["key"]: m for m in json.loads(next(l for l in again.output.splitlines() if l.startswith("{")))["members"]}
    assert rows["alpha"]["state"] == "already_merged", rows["alpha"]
    assert rows["beta"]["refused"].startswith("feature_moved"), rows["beta"]
    assert rows["beta"]["next"].startswith("next: "), rows["beta"]
    assert mains(slice2)["beta"] == beta["base"], "the moved member's target did not move"


def test_a_moved_member_after_a_half_land_gets_its_new_pin_and_the_waiting_members_in_its_next(tmp_path, monkeypatch):
    # The moved member's next is the new pin for that member (by --path), and the members the refusal left unpushed
    # ride on it, each named in the "(then ..., not pushed after the refusal" clause.
    s = make_slice(tmp_path, monkeypatch, ("alpha", "beta", "gamma"))
    record_set(s)
    beta = s["members"]["beta"]
    hook = beta["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'main is closed for now' >&2\nexit 1\n")
    hook.chmod(0o755)
    argv = ["review", "merge", s["review"], "--from", "feat", "--into", "main", "--approvals", "0"]
    first = runner.invoke(app, argv)
    assert first.exit_code == merge_gate.EXIT_PARTIAL, first.output
    hook.unlink()
    move(beta)
    again = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\n{again.output}exit {again.exit_code}")
    assert again.exit_code == merge_gate.EXIT_PARTIAL, again.output
    rows = {m["key"]: m for m in json.loads(next(l for l in again.output.splitlines() if l.startswith("{")))["members"]}
    assert rows["beta"]["refused"].startswith("feature_moved"), rows["beta"]
    step = rows["beta"].get("next", "")
    assert "gr2 review pin" in step and "--path beta" in step, step
    assert "(then gamma, not pushed after the refusal" in step, step


def _half_land(s, approvals="0"):
    """alpha lands, a pre-receive hook on beta refuses its push; the hook is then removed (the cause clears)."""
    hook = s["members"]["beta"]["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'main is closed for now' >&2\nexit 1\n")
    hook.chmod(0o755)
    first = runner.invoke(app, ["review", "merge", s["review"], "--from", "feat", "--into", "main", "--approvals", approvals])
    assert first.exit_code == merge_gate.EXIT_PARTIAL, first.output
    hook.unlink()


def _stamp(s, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "Approver B")
    stamped = runner.invoke(app, ["review", "stamp", s["review"]])
    print(f"$ gr2 review stamp {s['review']}\n{stamped.output}exit {stamped.exit_code}")
    return stamped


def _rerun_with_one_approval(s):
    argv = ["review", "merge", s["review"], "--from", "feat", "--into", "main", "--approvals", "1"]
    again = runner.invoke(app, argv)
    print(f"$ gr2 {' '.join(argv)}\n{again.output}exit {again.exit_code}")
    return again


def test_a_stamp_after_a_half_land_counts_and_the_rerun_finishes(slice2, monkeypatch):
    # With approvals required, the recovery for a half-landed review is stamp, then rerun. The landed member's head
    # is already on its target; the stamp still signs while another member has yet to land.
    record_set(slice2)
    _half_land(slice2)
    stamped = _stamp(slice2, monkeypatch)
    assert stamped.exit_code == 0, stamped.output
    assert "head_already_on_target" not in stamped.output and "no gr2 command lands it again" not in stamped.output
    again = _rerun_with_one_approval(slice2)
    assert again.exit_code == merge_gate.EXIT_MERGED, again.output
    rerun = {m["key"]: m["state"] for m in json.loads(next(l for l in again.output.splitlines() if l.startswith("{")))["members"]}
    assert rerun == {"alpha": "already_merged", "beta": "merged"}, rerun


def test_a_stamp_after_every_member_landed_still_refuses_head_already_on_target(slice2, monkeypatch):
    record_set(slice2)
    done = runner.invoke(app, ["review", "merge", slice2["review"], "--from", "feat", "--into", "main", "--approvals", "0"])
    assert done.exit_code == merge_gate.EXIT_MERGED, done.output
    stamped = _stamp(slice2, monkeypatch)
    assert stamped.exit_code == 2, stamped.output
    text = " ".join(stamped.output.split())
    assert "head_already_on_target: alpha: the reviewed head" in text, text
    assert "next: nothing to merge; the reviewed head is already in the target" in text, text


def test_a_stamp_before_the_half_land_is_counted_by_the_rerun(slice2, monkeypatch):
    # The control: stamped before anything landed, the same half-land and rerun reach exit 0.
    record_set(slice2)
    stamped = _stamp(slice2, monkeypatch)
    assert stamped.exit_code == 0, stamped.output
    _half_land(slice2, approvals="1")
    again = _rerun_with_one_approval(slice2)
    assert again.exit_code == merge_gate.EXIT_MERGED, again.output
    rerun = {m["key"]: m["state"] for m in json.loads(next(l for l in again.output.splitlines() if l.startswith("{")))["members"]}
    assert rerun == {"alpha": "already_merged", "beta": "merged"}, rerun


def test_the_readme_half_land_paragraph_names_the_stamp_before_the_rerun():
    from pathlib import Path
    text = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text().split())
    assert ("When the merge requires approvals, `gr2 review stamp` the same review before the rerun if the review is not stamped yet: it stamps while "
            "any member has yet to land.") in text

"""One passing `check set` merges a multi-member review when no member has a `test` of its own.

Measured on the dev wheel (one agent, one feature through 3 members): pin, publish, `check set`, one approval, then
`review merge` refused `check_absent`: the default requires `test` beside `set` (dcd4fe83), and no member had one. Its
next step was a per-member `gr2 check run`, which cannot pass when the members import each other. A default `test` that
is ABSENT at a member's head now yields to this review's passing set; a RED one still refuses (dcd4fe83's reason).

On a review of ONE member, `check set` over that member is its test at that head, so a passing `set` record for this
review and set of heads decides the default `test` there. A `set` record for another review, or for this review at
other heads, never counts, and a `test` the merger names is decided only by `test` records.

The multi-member fixture records NO per-member `test`: the shared slice2 fixture records one on every member, which is
how the earlier control merged while a real author's run refused.
"""
from __future__ import annotations

import json
import sys

import pytest

from gr2.python_cli import check_records, merge_gate, set_check
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX
from tests.test_review_merge_gate import git, publish, runner, world  # noqa: F401  (fixtures)

PASS = [sys.executable, "-c", "pass"]


@pytest.fixture
def bare_slice(tmp_path, monkeypatch):
    """Two members bound into one review, published, with NO check recorded on either."""
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
                     "ref": "refs/heads/main", "source": str(m["repo"]), "visibility": "public"})
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "bind", "--rows-json", str(tmp_path / "rows.json")])
    assert made.exit_code == 0, made.output
    out["review"] = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    for m in out["members"].values():
        git(m["repo"], "push", m["remote"], "feat")
        publish(author, out["review"], m["remote"])
    return out


def _mains(s):
    return {k: git(m["remote"], "rev-parse", "main") for k, m in s["members"].items()}


def _bases(s):
    return {k: m["base"] for k, m in s["members"].items()}


def _no_test_record(repo, remote, head):
    rows = check_records.read_remote_check(str(remote), {"path": str(repo), "key": None, "remote": str(remote)},
                                           head, ("test",))["records"]
    assert not [r for r in rows if r.get("name") == "test"], rows   # the precondition the earlier control lacked


def _forge_set(repo, remote, head, review, set_id):
    """A passing `set` record at `head` carrying the given review and set identity, the shape check set writes."""
    check_records.publish_observation(repo, str(remote), dict(
        v=1, head=head, observed_head=head, name="set", result="pass", exit_code=0, review=review, set_id=set_id,
        members=[f"member:{head}"], reproduced=False))


def _world_set_id(w):
    return set_check.set_id(merge_gate.grip.show_review_commit(w["author"], w["review"].removeprefix("gr:"))["members"])


# ---- several members ----------------------------------------------------------------------------------------------

def test_a_passing_set_alone_merges_a_multi_member_review(bare_slice):
    for m in bare_slice["members"].values():
        _no_test_record(m["repo"], m["remote"], m["head"])
    rec = set_check.run_set_check(bare_slice["author"], bare_slice["review"], "set", PASS)
    assert rec.get("result") == "pass", rec
    code, receipt = merge_gate.review_merge(bare_slice["author"], bare_slice["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt
    assert _mains(bare_slice) == {k: m["head"] for k, m in bare_slice["members"].items()} or \
        all(v != _bases(bare_slice)[k] for k, v in _mains(bare_slice).items()), receipt


def test_a_named_test_beside_only_a_set_record_refuses_and_names_a_run_that_can_pass(bare_slice):
    set_check.run_set_check(bare_slice["author"], bare_slice["review"], "set", PASS)
    code, receipt = merge_gate.review_merge(bare_slice["author"], bare_slice["review"], feature="feat",
                                            required_checks=("test",), required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert _mains(bare_slice) == _bases(bare_slice)
    for row in receipt["members"]:
        assert row["refused"].startswith("check_absent"), row
        assert f"gr2 check set {bare_slice['author']} {bare_slice['review']} --name test --" in row["next"], row["next"]
        assert "gr2 check run" not in row["next"], row["next"]


def test_a_set_record_for_another_review_does_not_merge_a_multi_member_review(bare_slice):
    for m in bare_slice["members"].values():
        _forge_set(m["repo"], m["remote"], m["head"], "gr:" + "0" * 40, "f" * 64)
    code, receipt = merge_gate.review_merge(bare_slice["author"], bare_slice["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert _mains(bare_slice) == _bases(bare_slice)


def test_a_red_member_test_beside_a_green_set_still_refuses(bare_slice):
    a = bare_slice["members"]["alpha"]
    check_records.run_check(a["repo"], str(a["remote"]), a["head"], "test", [sys.executable, "-c", "raise SystemExit(1)"])
    set_check.run_set_check(bare_slice["author"], bare_slice["review"], "set", PASS)
    code, receipt = merge_gate.review_merge(bare_slice["author"], bare_slice["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert _mains(bare_slice) == _bases(bare_slice)
    rows = {r["key"]: r for r in receipt["members"]}
    assert rows["alpha"]["refused"].startswith("check_fail"), receipt


def test_a_red_test_at_an_old_head_and_none_at_the_pinned_head_merges_on_the_set(bare_slice):
    # the binding is the member's head: a failure recorded at its parent is not a record at this head
    a = bare_slice["members"]["alpha"]
    old = git(a["repo"], "rev-parse", "HEAD~1")
    check_records.run_check(a["repo"], str(a["remote"]), old, "test", [sys.executable, "-c", "raise SystemExit(1)"])
    _no_test_record(a["repo"], a["remote"], a["head"])
    set_check.run_set_check(bare_slice["author"], bare_slice["review"], "set", PASS)
    code, receipt = merge_gate.review_merge(bare_slice["author"], bare_slice["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt


# ---- one member ---------------------------------------------------------------------------------------------------

def test_one_member_a_set_record_for_this_review_and_head_decides_the_default_test(world):  # noqa: F811
    _no_test_record(world["member"], world["remote"], world["head"])
    _forge_set(world["member"], world["remote"], world["head"], world["review"], _world_set_id(world))
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt
    assert receipt["members"][0]["check"]["source"] == {"test": "set"}, receipt


def test_one_member_a_set_record_for_another_review_is_refused(world):  # noqa: F811
    _forge_set(world["member"], world["remote"], world["head"], "gr:" + "0" * 40, _world_set_id(world))
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("check_absent"), receipt


def test_one_member_a_set_record_for_this_review_at_other_heads_is_refused(world):  # noqa: F811
    _forge_set(world["member"], world["remote"], world["head"], world["review"], "e" * 64)
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("check_absent"), receipt


def test_one_member_a_named_test_is_not_decided_by_a_set_record(world):  # noqa: F811
    _forge_set(world["member"], world["remote"], world["head"], world["review"], _world_set_id(world))
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_checks=("test",), required_approvals=0)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert receipt["members"][0]["refused"].startswith("check_absent"), receipt
    assert "gr2 check run" in receipt["members"][0]["next"], receipt   # one member: a run of it alone can pass


def test_control_one_member_a_test_record_still_merges(world):  # noqa: F811
    check_records.run_check(world["member"], str(world["remote"]), world["head"], "test", PASS)
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt


def test_the_merge_names_each_member_it_accepted_on_the_set_alone(bare_slice):
    a = bare_slice["members"]["alpha"]
    check_records.run_check(a["repo"], str(a["remote"]), a["head"], "test", PASS)   # alpha has its own test
    set_check.run_set_check(bare_slice["author"], bare_slice["review"], "set", PASS)
    out = runner.invoke(app, ["review", "merge", str(bare_slice["author"]), bare_slice["review"], "--approvals", "0"])
    text = out.output + (getattr(out, "stderr", "") or "")
    assert out.exit_code == 0, text
    assert "accepted on the set alone (no test record of its own at its head): beta;" in text, text

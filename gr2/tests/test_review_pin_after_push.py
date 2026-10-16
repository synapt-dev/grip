"""`gr2 review pin` of a head that was pushed before it was pinned, with no destructive path.

An equal tip on a branch other than --ref pins, and the bind records the order (pushed_before_pin) in its
own message, so the order is part of the gr id. With --source, a remote branch past the head (ahead) or
beside it (diverged) refuses before any write, judged by the remote's tip read at pin time (ls-remote),
never by a refs/remotes tracking copy. Real git and bare local remotes; the target branch is `trunk`."""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import next_steps
from gr2.python_cli.app import app
from tests.native_root_helper import native_root

runner = CliRunner()
SEEN: list[str] = []  # every CLI output of this module's rows, for P7
DESTRUCTIVE = re.compile(r"push\s+--delete|--force|push\s+-f\b| :refs/|\+refs/")


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "PATH": os.environ.get("PATH", "")}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _world(tmp_path: Path, *, push_head: bool = True) -> dict:
    """A bare remote with trunk at BASE; the author's clone `src` on `feat` at HEAD, tracking origin/feat.
    With push_head, feat is pushed (so the remote's refs/heads/feat tip == HEAD) before any pin."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(remote))
    src = tmp_path / "src"
    _git(tmp_path, "init", "-q", "-b", "trunk", str(src))
    base = _commit(src, "a")
    _git(src, "remote", "add", "origin", str(remote))
    _git(src, "push", "-q", "origin", "trunk")
    _git(src, "checkout", "-qb", "feat")
    head = _commit(src, "b")
    if push_head:
        _git(src, "push", "-q", "-u", "origin", "feat")
    ws = native_root(tmp_path / "ws")
    _git(ws, "config", "user.name", "t")
    _git(ws, "config", "user.email", "t@e")
    return {"remote": remote, "src": src, "ws": ws, "base": base, "head": head}


def _pin(w: dict, *extra: str, head: str | None = None, base: str | None = None, source: bool = True,
         ref: str = "refs/heads/trunk"):
    argv = ["review", "pin", str(w["ws"]), "--repo", "src", "--remote", str(w["remote"]),
            "--base", base or w["base"], "--head", head or w["head"], "--ref", ref]
    if source:
        argv += ["--source", str(w["src"])]
    result = runner.invoke(app, [*argv, *extra])
    SEEN.append(result.output)
    return result


def _gr_id(result) -> str:
    found = re.search(r"gr:[0-9a-f]{40}", result.output)
    assert found and result.exit_code == 0, f"pin did not succeed: {result.output}"
    return found.group(0)


def _show(ws: Path, rid: str) -> dict:
    result = runner.invoke(app, ["review", "show", str(ws), rid, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _review_refs(ws: Path) -> str:
    return _git(ws, "for-each-ref", "refs/dev.synapt.grip/")


def _advance_remote_feat(tmp_path: Path, w: dict, name: str) -> str:
    """Another clone moves the remote's feat one commit past HEAD; the author's clone is not told."""
    other = tmp_path / f"other-{name}"
    _git(tmp_path, "clone", "-q", "-b", "feat", str(w["remote"]), str(other))
    tip = _commit(other, name)
    _git(other, "push", "-q", "origin", "feat")
    return tip


def test_p1_equal_tip_on_another_branch_pins(tmp_path):
    w = _world(tmp_path)
    result = _pin(w)
    assert result.exit_code == 0, result.output
    assert _gr_id(result)


def test_p2_bind_records_pushed_before_pin_and_a_pre_push_pin_records_none(tmp_path):
    w = _world(tmp_path)
    member = _show(w["ws"], _gr_id(_pin(w)))["members"][0]
    assert member["pushed_before_pin"] == "refs/heads/feat"
    assert member["head"] == w["head"]
    # A head the remote has not seen yet (its feat is behind it): the usual order, no field.
    newer = _commit(w["src"], "c")
    fresh = _pin(w, head=newer)
    assert fresh.exit_code == 0, fresh.output
    assert "pushed_before_pin" not in _show(w["ws"], _gr_id(fresh))["members"][0]


def test_p3_ahead_refuses_and_names_the_remote_tip(tmp_path):
    w = _world(tmp_path)
    tip = _commit(w["src"], "c")
    _git(w["src"], "push", "-q", "origin", "feat")
    result = _pin(w)  # pin the older HEAD while the remote's feat holds tip
    assert result.exit_code == 2, result.output
    assert "refused: head_behind_remote_branch" in result.output
    assert f"--head {tip}" in result.output
    assert _review_refs(w["ws"]) == ""


def test_p4_diverged_refuses_without_a_write(tmp_path):
    w = _world(tmp_path)
    _advance_remote_feat(tmp_path, w, "theirs")
    _git(w["src"], "fetch", "-q", "origin")
    mine = _commit(w["src"], "mine")  # a local head beside the remote's tip
    result = _pin(w, head=mine)
    assert result.exit_code == 2, result.output
    assert "refused: remote_branch_diverged" in result.output
    assert "merge or rebase" in result.output
    assert _review_refs(w["ws"]) == ""


def test_p5_head_only_at_ref_tag_or_review_ref_still_refuses(tmp_path):
    w = _world(tmp_path, push_head=False)
    # At --ref itself: the head is the target branch's tip (base == head), in every spelling ls-remote matches.
    for ref in ("refs/heads/trunk", "heads/trunk", "trunk"):
        at_ref = _pin(w, head=w["base"], ref=ref)
        assert at_ref.exit_code == 2 and "head_already_on_remote" in at_ref.output, (ref, at_ref.output)
    # At a tag, then at a review-namespace ref: neither is a branch the head was pushed to.
    for target in ("refs/tags/v1", "refs/dev.synapt.grip/reviews/x"):
        _git(w["src"], "push", "-q", "origin", f"{w['head']}:{target}")
        result = _pin(w)
        assert result.exit_code == 2 and "head_already_on_remote" in result.output, (target, result.output)
        _git(w["src"], "push", "-q", "origin", f":{target}")  # test cleanup of its own fixture ref
    assert _review_refs(w["ws"]) == ""


def test_p5b_a_ref_matching_two_branches_refuses_and_an_exact_name_wins(tmp_path):
    # ls-remote prints refs/heads/a/trunk before refs/heads/trunk, so its first line is not a choice.
    w = _world(tmp_path, push_head=False)
    _git(w["src"], "push", "-q", "origin", f"{w['base']}:refs/heads/a/trunk")
    _git(w["src"], "push", "-q", "origin", f"{w['head']}:refs/heads/trunk")  # the head is the target's tip
    ambiguous = _pin(w, ref="trunk")  # matches refs/heads/a/trunk (printed first) and refs/heads/trunk
    assert ambiguous.exit_code == 2 and "invalid_field" in ambiguous.output, ambiguous.output
    assert "refs/heads/a/trunk" in ambiguous.output and "refs/heads/trunk" in ambiguous.output, ambiguous.output
    # heads/trunk matches only refs/heads/trunk, whose tip is the head, so the base is not its live head.
    one = _pin(w, ref="heads/trunk")
    assert one.exit_code == 2 and "base_not_live_head" in one.output, one.output
    exact = _pin(w, ref="refs/heads/trunk", base=w["head"], head=w["head"])
    assert exact.exit_code == 2 and "head_already_on_remote" in exact.output, exact.output
    assert _review_refs(w["ws"]) == ""


def test_p6_publish_after_pin_carries_the_order_and_leaves_the_branch(tmp_path):
    w = _world(tmp_path)
    rid = _gr_id(_pin(w))
    published = runner.invoke(app, ["review", "publish", str(w["ws"]), rid, "--remote", str(w["remote"])])
    SEEN.append(published.output)
    assert published.exit_code == 0, published.output
    assert _git(w["remote"], "rev-parse", "refs/heads/feat") == w["head"]
    # A reviewer's workspace that receives the id sees the same order: it is part of the id.
    theirs = native_root(tmp_path / "theirs")
    got = runner.invoke(app, ["review", "receive", str(theirs), rid, "--remote", str(w["remote"])])
    assert got.exit_code == 0, got.output
    assert _show(theirs, rid)["members"][0]["pushed_before_pin"] == "refs/heads/feat"


def test_p6b_bare_publish_after_pin_carries_the_order(tmp_path):
    # The bare publish form: no --remote, each member publishes to its own recorded remote.
    # The bare form pushes from the member clone at <ws>/<key>, so the author's clone sits there.
    w = _world(tmp_path)
    _git(tmp_path, "clone", "-q", "-b", "feat", str(w["remote"]), str(w["ws"] / "src"))
    w["src"] = w["ws"] / "src"
    rid = _gr_id(_pin(w))
    published = runner.invoke(app, ["review", "publish", str(w["ws"]), rid])
    SEEN.append(published.output)
    assert published.exit_code == 0, published.output
    assert _git(w["remote"], "rev-parse", "refs/heads/feat") == w["head"]
    theirs = native_root(tmp_path / "theirs")
    got = runner.invoke(app, ["review", "receive", str(theirs), rid, "--remote", str(w["remote"])])
    assert got.exit_code == 0, got.output
    assert _show(theirs, rid)["members"][0]["pushed_before_pin"] == "refs/heads/feat"


def test_p7_no_destructive_path_in_output_or_next_steps(tmp_path):
    del SEEN[:]
    for row in (test_p1_equal_tip_on_another_branch_pins, test_p3_ahead_refuses_and_names_the_remote_tip,
                test_p4_diverged_refuses_without_a_write, test_p5_head_only_at_ref_tag_or_review_ref_still_refuses,
                test_p6_publish_after_pin_carries_the_order_and_leaves_the_branch,
                test_p6b_bare_publish_after_pin_carries_the_order):
        (tmp_path / row.__name__).mkdir()
        row(tmp_path / row.__name__)
    assert sum("next:" in out for out in SEEN) >= 5, SEEN
    texts = SEEN + [next_steps.head_behind_remote_branch("0" * 40), next_steps.remote_branch_diverged(),
                    next_steps.head_already_on_remote(), inspect.getsource(next_steps)]
    hits = [t for t in texts if DESTRUCTIVE.search(t)]
    assert not hits, hits


def test_p8_ratified_names_a_real_receipt_or_refuses(tmp_path):
    # --ratified must name a stamped earlier pin of this head (test_review_pin_ratified.py
    # r4 pins with one); a string that names no pin refuses before any write.
    w = _world(tmp_path, push_head=False)
    _git(w["src"], "push", "-q", "origin", f"{w['head']}:refs/tags/v1")
    assert _pin(w).exit_code == 2
    result = _pin(w, "--ratified", "rcpt-1")
    assert result.exit_code == 2 and "ratified_receipt_unknown" in result.output, result.output


def test_p9_stale_tracking_ref_is_not_the_judge(tmp_path):
    w = _world(tmp_path)
    # The author's refs/remotes/origin/feat still says HEAD; the remote has moved past it.
    assert _git(w["src"], "rev-parse", "refs/remotes/origin/feat") == w["head"]
    _advance_remote_feat(tmp_path, w, "later")
    assert _git(w["src"], "rev-parse", "refs/remotes/origin/feat") == w["head"]
    result = _pin(w)
    assert result.exit_code == 2, result.output
    assert "refused: head_behind_remote_branch" in result.output
    assert _review_refs(w["ws"]) == ""


def _range_patch(tmp_path: Path, w: dict) -> Path:
    patch = tmp_path / "range.patch"
    patch.write_text(_git(w["src"], "format-patch", "--stdout", "--full-index", f"{w['base']}..{w['head']}") + "\n")
    return patch


def test_p10_a_hold_branch_pins_from_range_and_opens(tmp_path):
    # A head held on a branch under refs/heads/hold/ before review: the reviewer pins it from the
    # author's range (no --source), opens a lane on it, and the bind records where it was pushed.
    w = _world(tmp_path, push_head=False)
    _git(w["src"], "push", "-q", "origin", f"{w['head']}:refs/heads/hold/cfg")
    rid = _gr_id(_pin(w, "--from-range", str(_range_patch(tmp_path, w)), source=False))
    assert _show(w["ws"], rid)["members"][0]["pushed_before_pin"] == "refs/heads/hold/cfg"
    opened = runner.invoke(app, ["review", "open", str(w["ws"]), rid.removeprefix("gr:"), "--repo", "src",
                                 "--lane-dir", str(tmp_path / "lane"), "--enter", "--json"])
    assert opened.exit_code == 0, opened.output


def test_p10b_a_hold_ref_outside_refs_heads_still_refuses(tmp_path):
    # refs/hold/<x> is not a branch: the head counts as already on the remote, and nothing is written.
    w = _world(tmp_path, push_head=False)
    _git(w["src"], "push", "-q", "origin", f"{w['head']}:refs/hold/cfg")
    result = _pin(w, "--from-range", str(_range_patch(tmp_path, w)), source=False)
    assert result.exit_code == 2, result.output
    assert "refused: head_already_on_remote" in result.output
    assert "pin a head before you push it" in result.output
    assert _review_refs(w["ws"]) == ""

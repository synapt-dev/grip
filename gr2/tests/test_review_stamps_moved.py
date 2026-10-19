"""`review stamps` and `--since-my-stamp` after a member's remote feature branch moves: the stamp still counts (it is
valid for the pin), and each member says whether its branch left the pin, as blockers and merge will refuse."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from gr2.python_cli import grip, merge_gate
from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, git, slice2  # noqa: F401  (fixtures)
from tests.test_set_check_remote_heads import move_elsewhere

runner = CliRunner()


def stamp(s):
    git(s["author"], "config", "user.name", "Approver B")
    assert runner.invoke(app, ["review", "approve", s["review"]]).exit_code == 0


def read(s, *extra):
    out = runner.invoke(app, ["review", "stamps", s["review"], *extra])
    return out, json.loads(out.stdout), {m["key"]: m for m in json.loads(out.stdout)["members"]}


def test_stamps_names_the_moved_member_and_keeps_the_count(slice2, tmp_path):
    stamp(slice2)
    moved = move_elsewhere(tmp_path, slice2["members"]["beta"], "beta")
    out, body, rows = read(slice2)
    assert out.exit_code == 0, out.output
    assert body["count"] == 1  # the stamp is valid for the pin
    assert rows["beta"]["moved_since_pin"] is True and rows["beta"]["branch_head"] == moved
    assert rows["alpha"]["moved_since_pin"] is False and rows["alpha"]["branch_head"] == slice2["members"]["alpha"]["head"]
    assert rows["alpha"]["feature"] == rows["beta"]["feature"] == "feat"  # the branch compared, named per row
    beta_head = slice2["members"]["beta"]["head"]
    assert (f"beta moved since the pin: stamps cover {beta_head}, the branch feat is at {moved}; "
            "blockers and merge will refuse") in out.stderr
    assert "alpha" not in out.stderr.replace("resolved:", "")


def test_stamps_without_a_move_says_false_and_no_line(slice2):
    stamp(slice2)
    out, body, rows = read(slice2)
    assert out.exit_code == 0, out.output
    assert [rows[k]["moved_since_pin"] for k in ("alpha", "beta")] == [False, False]
    assert "moved since the pin" not in out.stderr and "unknown" not in out.stderr


def test_stamps_with_an_unreachable_remote_refuses_whole_and_never_says_false(slice2):
    # The approval count already reads every member remote, so an unreachable one makes `stamps` not measurable.
    stamp(slice2)
    beta = slice2["members"]["beta"]["remote"]
    beta.rename(beta.with_name("gone.git"))
    out = runner.invoke(app, ["review", "stamps", slice2["review"]])
    assert out.exit_code == 5, out.output
    assert "refused" in json.loads(out.stdout) and "moved_since_pin" not in out.stdout


def test_an_unreadable_remote_is_unknown_never_false(slice2):
    beta = slice2["members"]["beta"]["remote"]
    beta.rename(beta.with_name("gone.git"))
    members = grip.show_review_commit(slice2["author"], slice2["review"][3:])["members"]
    moves = merge_gate.feature_moves(slice2["author"], members, "feat")
    assert moves["beta"]["moved_since_pin"] is None and moves["beta"]["branch_head"] is None
    assert moves["beta"]["unmeasured"].startswith("RuntimeError")
    assert str(beta) not in moves["beta"]["unmeasured"]  # the exception text names git's argv, the URL included
    assert moves["alpha"]["moved_since_pin"] is False


def test_stamps_with_members_on_different_branches_says_unknown(slice2):
    stamp(slice2)
    git(slice2["members"]["beta"]["repo"], "checkout", "-q", "-b", "other")
    out, body, rows = read(slice2)
    assert out.exit_code == 0, out.output
    assert [rows[k]["moved_since_pin"] for k in ("alpha", "beta")] == [None, None]
    assert rows["alpha"]["unmeasured"].startswith("feature_unresolved")
    assert rows["alpha"]["feature"] is None
    assert "alpha: unknown whether the branch moved since the pin (feature_unresolved" in out.stderr
    assert "moved since the pin: stamps cover" not in out.stderr


def test_stamps_names_a_deleted_branch_as_moved(slice2):
    stamp(slice2)
    git(slice2["members"]["beta"]["repo"], "push", "-q", str(slice2["members"]["beta"]["remote"]), ":feat")
    out, body, rows = read(slice2)
    assert rows["beta"]["moved_since_pin"] is True and rows["beta"]["branch_head"] is None
    assert "the branch feat is at nothing (absent)" in out.stderr


def test_since_my_stamp_names_the_live_move_on_the_same_pin(slice2, tmp_path):
    stamp(slice2)
    moved = move_elsewhere(tmp_path, slice2["members"]["beta"], "beta")
    out, body, rows = read(slice2, "--since-my-stamp")
    assert out.exit_code == 0, out.output
    assert rows["beta"]["changed"] is False  # pin to pin, unchanged meaning
    assert rows["beta"]["moved_since_pin"] is True and rows["beta"]["branch_head"] == moved
    assert rows["alpha"]["moved_since_pin"] is False
    assert "beta moved since the pin" in out.stderr and "alpha moved" not in out.stderr


def test_a_credentialed_remote_is_unknown_and_never_printed(slice2, monkeypatch, capsys):
    from gr2.python_cli import app as appmod
    members = grip.show_review_commit(slice2["author"], slice2["review"][3:])["members"]
    [beta] = [m for m in members if m["key"] == "beta"]
    beta["remote"] = "https://user:s3cret-value@127.0.0.1:9/beta.git"
    monkeypatch.setattr(grip, "show_review_commit", lambda ws, rid: {"members": members})
    rows = [{"key": m["key"], "head": m["head"]} for m in members]
    appmod._stamps_moved(slice2["author"], slice2["review"][3:], rows, "head")
    cap = capsys.readouterr()
    assert "s3cret-value" not in json.dumps(rows) + cap.out + cap.err
    beta_row = {r["key"]: r for r in rows}["beta"]
    assert beta_row["moved_since_pin"] is None and beta_row["unmeasured"].startswith("remote_credentials")
    assert {r["key"]: r for r in rows}["alpha"]["moved_since_pin"] is False


def test_a_member_row_not_in_the_pin_is_unknown_never_false(slice2, capsys):
    from gr2.python_cli import app as appmod
    rows = [{"key": "alpha", "head": slice2["members"]["alpha"]["head"]}, {"key": "gamma", "head": "f" * 40}]
    appmod._stamps_moved(slice2["author"], slice2["review"][3:], rows, "head")
    gamma = {r["key"]: r for r in rows}["gamma"]
    assert gamma["moved_since_pin"] is None and gamma["unmeasured"] == "not in this pin" and gamma["feature"] == "feat"
    assert "gamma: unknown whether the branch moved since the pin (not in this pin)" in capsys.readouterr().err

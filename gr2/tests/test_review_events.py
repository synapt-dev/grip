"""`review.*` events: a review lane shows up on the outbox the way a lane or a PR does, so
a timeline or a hook consumer can see reviews."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from gr2.python_cli.app import app
from gr2.python_cli import review_run as rr
from tests.test_review_cli import _bind, _fixture_repo, _grip_ws
from tests.conftest import make_cli_runner

runner = make_cli_runner()
from tests.test_review_run import PASS_TEST, _git, _offline_install, _pkg_repo, _write_marker


def _outbox(ws: Path) -> list[dict]:
    path = ws / ".grip" / "events" / "outbox.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_review_open_emits_review_opened(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)
    lane = tmp_path / "lane"
    opened = runner.invoke(
        app, ["review", "open", str(ws), grc, "--repo", "recall", "--lane-dir", str(lane), "--enter"],
    )
    assert opened.exit_code == 0, opened.output
    events = [e for e in _outbox(ws) if e["type"] == "review.opened"]
    assert len(events) == 1, _outbox(ws)
    ev = events[0]
    assert ev["bind_commit"] == grc
    assert ev["lane_dir"] == str(lane.resolve())
    assert ev["repos"] == {"recall": {"tree_match": True}}


def _types(ws: Path) -> list[str]:
    return [e["type"] for e in _outbox(ws) if e["type"].startswith("review.")]


def test_bind_verify_and_close_emit_and_the_marker_names_its_workspace(tmp_path):
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)
    bound = [e for e in _outbox(ws) if e["type"] == "review.bound"]
    assert [(e["bind_commit"], e["repos"]) for e in bound] == [(grc, ["recall"])]

    verified = runner.invoke(app, ["review", "verify", str(ws), grc])
    assert verified.exit_code == 0, verified.output
    ev = [e for e in _outbox(ws) if e["type"] == "review.verified"]
    assert [(e["bind_commit"], e["tree_matches"]) for e in ev] == [(grc, True)]

    lane = tmp_path / "lane"
    opened = runner.invoke(
        app, ["review", "open", str(ws), grc, "--repo", "recall", "--lane-dir", str(lane), "--enter"],
    )
    assert opened.exit_code == 0, opened.output
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    assert marker["workspace_root"] == str(ws.resolve())

    closed = runner.invoke(app, ["review", "close", str(lane)])
    assert closed.exit_code == 0, closed.output
    ev = [e for e in _outbox(ws) if e["type"] == "review.closed"]
    assert [(e["bind_commit"], e["lane_dir"]) for e in ev] == [(grc, str(lane.resolve()))]
    assert _types(ws) == ["review.bound", "review.verified", "review.opened", "review.closed"]


def _run_lane(tmp_path: Path, ws: Path, *, workspace_in_marker: bool) -> Path:
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    if workspace_in_marker:
        marker = json.loads((repo / rr._MARKER_NAME).read_text())
        marker["workspace_root"] = str(ws.resolve())
        (repo / rr._MARKER_NAME).write_text(json.dumps(marker))
    return repo


def test_review_run_green_emits_run_completed_with_counts(tmp_path):
    ws = _grip_ws(tmp_path)
    lane = _run_lane(tmp_path, ws, workspace_in_marker=True)
    result = runner.invoke(
        app, ["review", "run", str(lane), "--package", "demo_pkg",
              "--install", shlex.join(_offline_install(lane)), "--", "-q"],
    )
    assert result.exit_code == 0, result.output
    ev = [e for e in _outbox(ws) if e["type"] == "review.run_completed"]
    assert len(ev) == 1, _outbox(ws)
    assert ev[0]["result"] == "green" and ev[0]["passed"] >= 1 and ev[0]["failed"] == 0
    assert ev[0]["bind_commit"] == "gr:" + "deadbeef" * 5


def test_review_run_refusal_emits_run_refused_with_its_code(tmp_path):
    ws = _grip_ws(tmp_path)
    lane = _run_lane(tmp_path, ws, workspace_in_marker=True)
    result = runner.invoke(app, ["review", "run", str(lane), "--test", "true"])
    assert result.exit_code == 2, result.output
    ev = [e for e in _outbox(ws) if e["type"] == "review.run_refused"]
    assert [e["refusal_code"] for e in ev] == ["test_with_pytest"]


def test_a_lane_whose_marker_names_no_workspace_runs_and_emits_nothing(tmp_path):
    """A lane opened before the marker carried `workspace_root` has no outbox to write to:
    the run still works, and no event lands anywhere (in particular not in the lane)."""
    ws = _grip_ws(tmp_path)
    lane = _run_lane(tmp_path, ws, workspace_in_marker=False)
    result = runner.invoke(app, ["review", "run", str(lane), "--test", "true"])
    assert result.exit_code == 2, result.output
    assert _types(ws) == []
    assert not (lane / ".grip" / "events").exists()


def test_a_two_row_open_emits_one_review_opened_naming_both_rows(tmp_path):
    a = _fixture_repo(tmp_path, "a")
    b = _fixture_repo(tmp_path, "b")
    ws = _grip_ws(tmp_path)
    rows = [
        {"key": key, "remote": remote, "base": base, "head": head, "ref": "refs/heads/dev",
         "path": key, "source": str(work)}
        for key, (remote, base, head, work) in (("alpha", a), ("beta", b))
    ]
    rows_json = tmp_path / "rows.json"
    rows_json.write_text(json.dumps(rows))
    bound = runner.invoke(app, ["review", "bind", str(ws), "--rows-json", str(rows_json)])
    assert bound.exit_code == 0, bound.output
    grc = bound.stdout.strip()
    lane = tmp_path / "lane2"
    opened = runner.invoke(app, ["review", "open", str(ws), grc, "--lane-dir", str(lane), "--enter"])
    assert opened.exit_code == 0, opened.output
    ev = [e for e in _outbox(ws) if e["type"] == "review.opened"]
    assert len(ev) == 1, _outbox(ws)
    assert ev[0]["repos"] == {"alpha": {"tree_match": True}, "beta": {"tree_match": True}}


def test_a_lane_whose_recorded_workspace_is_gone_emits_nothing_and_creates_nothing(tmp_path):
    """The marker names a workspace that no longer exists. The run still answers, no event is
    written, and the vanished path is NOT recreated (emit makes the outbox's parents)."""
    ws = _grip_ws(tmp_path)
    lane = _run_lane(tmp_path, ws, workspace_in_marker=True)
    gone = tmp_path / "moved-away"
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    marker["workspace_root"] = str(gone)
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker))
    result = runner.invoke(app, ["review", "run", str(lane), "--test", "true"])
    assert result.exit_code == 2, result.output
    assert not gone.exists()
    closed = runner.invoke(app, ["review", "close", str(lane)])
    assert closed.exit_code != 0, closed.output
    assert "allocation" in closed.output
    assert lane.exists()
    assert not gone.exists()


def test_a_recorded_workspace_that_exists_without_grip_gets_nothing_written_into_it(tmp_path):
    """The recorded path exists but is not a workspace (no `.grip`). Existence alone is not the
    test: run and close emit nothing and write no `.grip` into that directory."""
    ws = _grip_ws(tmp_path)
    lane = _run_lane(tmp_path, ws, workspace_in_marker=True)
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    marker["workspace_root"] = str(plain)
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker))
    result = runner.invoke(app, ["review", "run", str(lane), "--test", "true"])
    assert result.exit_code == 2, result.output
    closed = runner.invoke(app, ["review", "close", str(lane)])
    assert closed.exit_code != 0, closed.output
    assert "allocation" in closed.output
    assert lane.exists()
    assert list(plain.iterdir()) == []


def test_verify_prints_its_verdict_even_when_the_outbox_cannot_be_written(tmp_path):
    """review.verified is strict, so an unwritable outbox fails the command; the verdict the
    caller asked for is printed before that, never swallowed."""
    remote, base, head, work = _fixture_repo(tmp_path)
    ws = _grip_ws(tmp_path)
    grc = _bind(ws, remote, base, head, work)
    events = ws / ".grip" / "events"
    for child in events.iterdir():
        child.unlink()
    events.rmdir()
    events.write_text("not a directory\n")
    verified = runner.invoke(app, ["review", "verify", str(ws), grc])
    assert verified.exit_code == 1
    assert "tree_matches: True" in verified.stdout
    assert "review.verified could not be recorded" in verified.stderr
    assert "Traceback" not in verified.output and "EventEmitError" not in verified.output

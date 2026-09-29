"""`review close-gr <lane-dir>`: a verb-owned teardown for an `open-gr --enter`
reconstruction lane (originating exit-point work tracked privately).

`open-gr --enter` is a pure reconstruction into `--lane-dir` — it pushes no lane
onto the return stack and changes no cwd, so `review exit-gr` (the open-PROJECT
pop, which needs OWNER_UNIT, a receipt, and a lane to pop) cannot apply and the
`--lane-dir` was left for a hand `rm`. This gives it a symmetric teardown: open-gr
writes a small marker, and `close-gr` reads it, refuses a directory that is NOT an
open-gr lane (so it never rm's an arbitrary path), and reclaims the tree.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli import open_gr_review


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _base_remote_and_range(tmp_path: Path) -> tuple[str, str, str, str]:
    """A bare origin whose main carries only BASE, plus a range.patch for a pre-push
    head. Returns (remote_url, base_sha, head_sha, range_patch_text)."""
    origin = tmp_path / "alpha.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")
    (work / "f.txt").write_text("base\nchange\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "head")
    head = _git(work, "rev-parse", "HEAD")
    range_patch = subprocess.run(
        ["git", "format-patch", f"{base}..{head}", "--stdout"],
        cwd=work, text=True, capture_output=True, check=True).stdout
    return str(origin), base, head, range_patch


def _open_gr_lane(tmp_path: Path, runner: CliRunner) -> tuple[Path, str]:
    """Bind a range and open-gr --enter into a lane. Returns (lane_dir, gr_sha)."""
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    from gr2.python_cli import grip
    grip.grip_init(ws)
    remote, base, head, range_patch = _base_remote_and_range(tmp_path)
    range_file = tmp_path / "range.patch"
    range_file.write_text(range_patch)
    res = runner.invoke(gr2_app.app, [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--from-range", str(range_file),
    ])
    assert res.exit_code == 0, res.output
    gr_sha = res.output.strip()[len("gr:"):]
    lane_dir = tmp_path / "lane"
    res2 = runner.invoke(gr2_app.app, [
        "review", "open-gr", str(ws), gr_sha, "--repo", "alpha",
        "--lane-dir", str(lane_dir), "--enter",
    ])
    assert res2.exit_code == 0, res2.output
    return lane_dir, gr_sha


def test_open_gr_enter_writes_a_reconstruct_marker(tmp_path: Path) -> None:
    runner = CliRunner()
    lane_dir, gr_sha = _open_gr_lane(tmp_path, runner)
    marker = lane_dir / open_gr_review._OPEN_GR_MARKER
    assert marker.exists(), "open-gr --enter must write a teardown marker in the lane"
    data = json.loads(marker.read_text())
    assert data["kind"] == "open-gr-reconstruct"
    assert data["gr_commit"] == gr_sha


def test_close_gr_reclaims_the_lane(tmp_path: Path) -> None:
    runner = CliRunner()
    lane_dir, gr_sha = _open_gr_lane(tmp_path, runner)
    assert lane_dir.exists()
    res = runner.invoke(gr2_app.app, ["review", "close-gr", str(lane_dir)])
    assert res.exit_code == 0, res.output
    assert not lane_dir.exists(), "close-gr must reclaim the open-gr lane tree"
    assert gr_sha[:12] in res.output, res.output  # names the commit it reclaimed


def test_open_gr_no_repo_single_row_puts_the_marker_at_the_tree(tmp_path: Path) -> None:
    # review-run door 3: with a SINGLE bound row and no --repo, open-gr used to lay the
    # clone under <lane-dir>/<key> while writing the marker at <lane-dir> -- one level
    # ABOVE the only tree -- so `review run <lane-dir>` found the marker but no git repo
    # and `review run <lane-dir>/<key>` found the repo but no marker. The single row with
    # no --repo must collapse to the --repo layout: clone AND marker at <lane-dir>.
    runner = CliRunner()
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    from gr2.python_cli import grip
    grip.grip_init(ws)
    remote, base, head, range_patch = _base_remote_and_range(tmp_path)
    range_file = tmp_path / "range.patch"
    range_file.write_text(range_patch)
    res = runner.invoke(gr2_app.app, [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--from-range", str(range_file),
    ])
    assert res.exit_code == 0, res.output
    gr_sha = res.output.strip()[len("gr:"):]

    lane_dir = tmp_path / "lane"
    # NO --repo
    res2 = runner.invoke(gr2_app.app, [
        "review", "open-gr", str(ws), gr_sha, "--lane-dir", str(lane_dir), "--enter",
    ])
    assert res2.exit_code == 0, res2.output
    # the marker and the reconstructed clone are BOTH at the lane root -- not nested
    assert (lane_dir / open_gr_review._OPEN_GR_MARKER).is_file(), "marker must be at the tree root"
    assert (lane_dir / ".git").exists(), "the reconstructed clone must be AT lane-dir, not a subdir"
    assert not (lane_dir / "alpha").exists(), "the single row must not be nested under <key>"
    # and the marker/clone alignment is exactly what `review run` and `close-gr` consume:
    marker = json.loads((lane_dir / open_gr_review._OPEN_GR_MARKER).read_text())
    assert marker["kind"] == "open-gr-reconstruct"
    res3 = runner.invoke(gr2_app.app, ["review", "close-gr", str(lane_dir)])
    assert res3.exit_code == 0, res3.output
    assert not lane_dir.exists()


def test_close_gr_keeps_the_review_run_receipt_and_log(tmp_path: Path) -> None:
    # review-run door 1 (close half): `review run` writes its receipt and pytest-output
    # log INSIDE the lane, so close-gr's rmtree would destroy the only record of a red
    # run (35 env failures, in the real review). close-gr must carry them OUT first, to
    # a path it names, that survives the rmtree — asserted to exist AFTER close.
    from gr2.python_cli import review_run as rr

    runner = CliRunner()
    lane_dir, gr_sha = _open_gr_lane(tmp_path, runner)
    # a red run's artifacts, as review_run would write them into the lane
    (lane_dir / rr._OUTPUT_LOG_NAME).write_text(
        "=== short test summary info ===\nFAILED tests/t.py::test_bad - boom\n"
    )
    (lane_dir / rr._RECEIPT_NAME).write_text(json.dumps({
        "kind": "review-run", "created": "2026-09-07T09:00:00+00:00",
        "gr_commit": gr_sha, "result": "red", "failed": 1,
        "failed_ids": ["tests/t.py::test_bad"],
        "output_log": rr._OUTPUT_LOG_NAME,
    }, indent=2) + "\n")

    res = runner.invoke(gr2_app.app, ["review", "close-gr", str(lane_dir), "--json"])
    assert res.exit_code == 0, res.output
    assert not lane_dir.exists(), "close-gr still reclaims the lane"

    result = json.loads(res.output)
    preserved = result["preserved_run"]
    receipt_path = Path(preserved["receipt"])
    log_path = Path(preserved["log"])
    # the evidence exists AFTER the rmtree, outside the (now gone) lane
    assert receipt_path.is_file(), "the run receipt must survive close-gr"
    assert log_path.is_file(), "the run output log must survive close-gr"
    assert lane_dir not in receipt_path.parents and lane_dir not in log_path.parents
    # the surviving receipt names its surviving log (the lane-relative name is dead)
    kept = json.loads(receipt_path.read_text())
    assert kept["failed_ids"] == ["tests/t.py::test_bad"]
    assert Path(kept["output_log"]) == log_path and Path(kept["output_log"]).is_file()
    assert "FAILED tests/t.py::test_bad" in log_path.read_text()


def test_close_gr_with_no_review_run_has_nothing_to_keep(tmp_path: Path) -> None:
    # A lane that was opened but never `review run` has no receipt: close-gr reclaims it
    # exactly as before and preserves nothing (no empty sibling dir, no crash).
    runner = CliRunner()
    lane_dir, gr_sha = _open_gr_lane(tmp_path, runner)
    res = runner.invoke(gr2_app.app, ["review", "close-gr", str(lane_dir), "--json"])
    assert res.exit_code == 0, res.output
    assert not lane_dir.exists()
    assert "preserved_run" not in json.loads(res.output)
    assert not (lane_dir.parent / f"{lane_dir.name}.review-run").exists()


def test_close_gr_twice_same_lane_keeps_both_runs(tmp_path: Path) -> None:
    # review-run door 1, a probe on the second version: closing the SAME lane name twice must preserve
    # BOTH runs' receipt+log. v1 keyed the preserved dir by lane name alone
    # (<lane>.review-run, mkdir exist_ok + copy2), so the second close overwrote the
    # first's evidence in place -- door 1's promise held for one close per lane name.
    # v2 keys by (gr short sha, run timestamp) from the receipt, so a reopen+rerun of
    # the same lane lands beside the first rather than on top of it.
    from gr2.python_cli import review_run as rr
    from gr2.python_cli import grip

    runner = CliRunner()
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    grip.grip_init(ws)
    remote, base, head, range_patch = _base_remote_and_range(tmp_path)
    range_file = tmp_path / "range.patch"
    range_file.write_text(range_patch)
    res = runner.invoke(gr2_app.app, [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--from-range", str(range_file),
    ])
    assert res.exit_code == 0, res.output
    gr_sha = res.output.strip()[len("gr:"):]
    lane_dir = tmp_path / "lane"

    def open_run_close(created: str, tag: str) -> dict:
        ro = runner.invoke(gr2_app.app, [
            "review", "open-gr", str(ws), gr_sha, "--repo", "alpha",
            "--lane-dir", str(lane_dir), "--enter",
        ])
        assert ro.exit_code == 0, ro.output
        (lane_dir / rr._OUTPUT_LOG_NAME).write_text(f"FAILED tests/t.py::{tag} - boom\n")
        (lane_dir / rr._RECEIPT_NAME).write_text(json.dumps({
            "kind": "review-run", "created": created, "gr_commit": gr_sha,
            "result": "red", "failed": 1, "failed_ids": [f"tests/t.py::{tag}"],
            "output_log": rr._OUTPUT_LOG_NAME,
        }, indent=2) + "\n")
        rc = runner.invoke(gr2_app.app, ["review", "close-gr", str(lane_dir), "--json"])
        assert rc.exit_code == 0, rc.output
        assert not lane_dir.exists()
        return json.loads(rc.output)["preserved_run"]

    p1 = open_run_close("2026-09-07T10:00:00+00:00", "test_first")
    p2 = open_run_close("2026-09-07T11:30:00+00:00", "test_second")

    # the two closes preserved to DISTINCT paths -- the second did not overwrite the first
    assert p1["receipt"] != p2["receipt"], (p1, p2)
    r1 = json.loads(Path(p1["receipt"]).read_text())
    r2 = json.loads(Path(p2["receipt"]).read_text())
    assert r1["failed_ids"] == ["tests/t.py::test_first"], r1
    assert r2["failed_ids"] == ["tests/t.py::test_second"], r2
    assert r1["created"] != r2["created"]
    # both logs survive, each with its own run's failures
    assert "test_first" in Path(p1["log"]).read_text()
    assert "test_second" in Path(p2["log"]).read_text()
    # and reading the FIRST receipt back still shows the first run (not clobbered)
    assert json.loads(Path(p1["receipt"]).read_text())["failed_ids"] == ["tests/t.py::test_first"]


def test_open_gr_enter_refuses_a_nonempty_lane_dir(tmp_path: Path) -> None:
    # Probe C (run): open-gr --enter into a PRE-EXISTING dir that holds
    # a foreign file must REFUSE, because close-gr reclaims the WHOLE --lane-dir. The
    # marker proves open-gr WROTE there, not that it CREATED the dir; without this guard
    # close-gr removes the foreign file. open-gr owns the lane or does not write it.
    runner = CliRunner()
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    from gr2.python_cli import grip
    grip.grip_init(ws)
    remote, base, head, range_patch = _base_remote_and_range(tmp_path)
    range_file = tmp_path / "range.patch"
    range_file.write_text(range_patch)
    res = runner.invoke(gr2_app.app, [
        "review", "bind", str(ws), "--repo", "alpha", "--remote", remote,
        "--base", base, "--head", head, "--ref", "refs/heads/main",
        "--from-range", str(range_file),
    ])
    assert res.exit_code == 0, res.output
    gr_sha = res.output.strip()[len("gr:"):]

    # --repo OMITTED: root is the mkdir'd PARENT (each row -> root/<key>) and the
    # marker lands at root/. Without the guard open-gr writes the lane UNDER the
    # pre-existing foreign file, and close-gr then rmtrees the whole root.
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "foreign.txt").write_text("keep me\n")
    res2 = runner.invoke(gr2_app.app, [
        "review", "open-gr", str(ws), gr_sha,
        "--lane-dir", str(shared), "--enter",
    ])
    assert res2.exit_code != 0, res2.output
    assert (shared / "foreign.txt").exists(), "open-gr must not write into a non-empty dir"
    assert not (shared / open_gr_review._OPEN_GR_MARKER).exists(), "no marker written"
    # and with no marker, close-gr also refuses — the foreign file is doubly safe.
    res3 = runner.invoke(gr2_app.app, ["review", "close-gr", str(shared)])
    assert res3.exit_code != 0
    assert (shared / "foreign.txt").exists()


def test_close_gr_refuses_a_dir_without_the_marker(tmp_path: Path) -> None:
    # Safety: close-gr must never rm a directory that is not an open-gr lane.
    runner = CliRunner()
    plain = tmp_path / "not-a-lane"
    plain.mkdir()
    (plain / "keep.txt").write_text("do not delete me\n")
    res = runner.invoke(gr2_app.app, ["review", "close-gr", str(plain)])
    assert res.exit_code != 0
    assert plain.exists(), "close-gr must not remove a directory lacking the marker"
    assert (plain / "keep.txt").exists()

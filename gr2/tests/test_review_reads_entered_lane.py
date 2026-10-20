"""With a lane entered, review pin, publish and blockers read the lane's clone, the one commit wrote to.

Before, all three joined the workspace root with the member path and read the shared checkout: pin found
nothing to pin, publish refused head_moved_since_pin, and blockers resolved --from to the shared branch.
With no lane entered every verb still reads the shared checkout; with several units in a lane they refuse
by name rather than guess.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import review_update
from tests.test_review_a4_v2 import _init, _invoke
from tests.test_review_a4_v3 import _spec
from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import two_member_ws  # noqa: F401


def _g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _lane(ws: Path, unit: str, *, enter: bool = True) -> Path:
    shared = ws / "alpha"
    assert lanes.create_lane(argparse.Namespace(
        workspace_root=ws, owner_unit=unit, lane_name="feature", type="feature",
        repos="alpha", branch="alpha=feat", source="test", default_commands=[],
    )) == 0
    lane_root = lanes.lane_checkout_root(ws, unit, "feature")
    (lane_root / "repos").mkdir(parents=True)
    clone = lane_root / "repos" / "alpha"
    subprocess.run(["git", "clone", "-q", str(shared), str(clone)], check=True)
    _g(clone, "checkout", "-q", "-b", "feat")
    if enter:
        entered = _invoke("lane", "enter", str(ws), unit, "feature", "--actor", "test")
        assert entered.exit_code == 0, entered.output
    return clone


@pytest.fixture
def ws_at_pin(two_member_ws: Path) -> tuple[Path, str, str, str]:
    ws = two_member_ws
    _init(ws)
    remote, base, head = _unpushed_head(ws)
    _spec(ws, remote, base, path="alpha", ref="main")
    spec = ws / ".grip" / "workspace_spec.toml"
    spec.write_text(spec.read_text() + "".join(
        f'\n[[units]]\nname = "{u}"\npath = "agents/{u}"\nrepos = ["alpha"]\n' for u in ("north", "south")))
    _g(ws / "alpha", "reset", "-q", "--hard", base)  # the shared checkout sits AT the pin
    return ws, remote, base, head


def _commit_in_lane(ws: Path, clone: Path) -> str:
    (clone / "lane.txt").write_text("lane\n")
    _g(clone, "add", "lane.txt")
    committed = _invoke("commit", "-m", "lane change", "--workspace-root", str(ws), "--owner-unit", "north")
    assert committed.exit_code == 0, committed.output
    return _g(clone, "rev-parse", "HEAD")


def _pin_row(ws: Path, remote: str, base: str, head: str, source: Path) -> str:
    rows = _invoke("review", "pin", str(ws), "--repo", "alpha", "--remote", remote.removeprefix("file://"), "--base", base,
                   "--head", head, "--ref", "refs/heads/main", "--source", str(source))
    assert rows.exit_code == 0, rows.stdout + rows.stderr
    return rows.stdout.strip().splitlines()[-1]


def test_pin_with_no_rows_reads_the_entered_lane(ws_at_pin) -> None:
    ws, _remote, base, _head = ws_at_pin
    lane_head = _commit_in_lane(ws, _lane(ws, "north"))
    pin = _invoke("review", "pin", str(ws))
    assert pin.exit_code == 0, pin.stdout + pin.stderr
    assert f"alpha {base[:4]}" in pin.stdout + pin.stderr or base in pin.stdout + pin.stderr
    assert lane_head[:4] in pin.stdout + pin.stderr
    assert pin.stderr.count("gr2: reading lane north/feature (entered)") == 1, pin.stderr


def test_publish_preflight_reads_the_entered_lane(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    pub = _invoke("review", "publish", str(ws), gr_id)
    out = pub.stdout + pub.stderr
    assert pub.exit_code == 0, out
    assert f"head=feat@{lane_head[:7]} (pushed)" in out


def test_blockers_resolves_from_the_entered_lane_branch(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    blk = _invoke("review", "blockers", str(ws), gr_id)
    out = blk.stdout + blk.stderr
    assert "from=feat (" in out, out
    assert blk.stderr.count("gr2: reading lane north/feature (entered)") == 1, blk.stderr


def test_blockers_with_no_review_id_finds_the_pin_by_the_lane_head(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    blk = _invoke("review", "blockers", str(ws))
    out = blk.stdout + blk.stderr
    assert "review_ambiguous" not in out, out  # the shared checkout's HEAD matches no pin; the lane's does
    assert f"review={gr_id} (pinned review at the current heads)" in out, out
    assert "from=feat (" in out, out


def test_several_units_in_a_lane_refuse_by_name(ws_at_pin) -> None:
    ws, _remote, _base, _head = ws_at_pin
    _lane(ws, "north")
    _lane(ws, "south")
    pin = _invoke("review", "pin", str(ws))
    out = pin.stdout + pin.stderr
    assert pin.exit_code != 0
    assert "south/feature" in out and "north/feature" in out and "gr2 lane exit" in out


def test_update_merges_in_the_entered_lane_it_preflights(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    mover = ws.parent / "mover"
    subprocess.run(["git", "clone", "-q", remote, str(mover)], check=True)
    (mover / "moved.txt").write_text("moved\n")
    _g(mover, "add", "moved.txt")
    _g(mover, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "target moved")
    _g(mover, "push", "-q", "origin", "HEAD:main")
    tip = _g(mover, "rev-parse", "HEAD")
    shared_before = _g(ws / "alpha", "rev-parse", "HEAD")
    assert shared_before != lane_head  # the two trees sit at different heads
    code, receipt = review_update.update(ws, gr_id)
    assert code == 0, receipt["refused"]
    assert _g(clone, "rev-parse", "HEAD^1", "HEAD^2").split() == [lane_head, tip]
    assert _g(ws / "alpha", "rev-parse", "HEAD") == shared_before  # the shared checkout is untouched
    assert receipt["members"][0]["head"] == _g(clone, "rev-parse", "HEAD")


def test_control_no_lane_entered_reads_the_shared_checkout(ws_at_pin) -> None:
    ws, remote, base, head = ws_at_pin
    shared = ws / "alpha"
    _lane(ws, "north", enter=False)
    pin = _invoke("review", "pin", str(ws))
    assert pin.exit_code != 0 or base not in pin.stdout  # the shared checkout sits at the pin: nothing to pin
    assert "reading lane" not in pin.stderr, pin.stderr
    _g(shared, "checkout", "-q", "-b", "shared-feat")
    (shared / "shared.txt").write_text("shared\n")
    _g(shared, "add", "shared.txt")
    _g(shared, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "shared change")
    shared_head = _g(shared, "rev-parse", "HEAD")
    gr_id = _pin_row(ws, remote, base, shared_head, shared)
    pub = _invoke("review", "publish", str(ws), gr_id)
    assert pub.exit_code == 0, pub.stdout + pub.stderr
    assert f"head=shared-feat@{shared_head[:7]} (pushed)" in pub.stdout + pub.stderr
    blk = _invoke("review", "blockers", str(ws), gr_id)
    assert "from=shared-feat (" in blk.stdout + blk.stderr
    assert "reading lane" not in pub.stderr + blk.stderr, pub.stderr + blk.stderr

"""With a lane entered, `check set` reads each member from the lane's clone, the one commit wrote to.

Before, it joined the workspace root with the member path and read the shared checkout, so a head committed in the
lane was not found there and the check refused head_not_in_member_clone. It now resolves members the way review pin,
publish and blockers do (commit.entered_member_roots); with no lane entered it still reads the shared checkout.
"""
from __future__ import annotations

import json

from tests.test_review_a4_v2 import _invoke
from tests.test_review_reads_entered_lane import _commit_in_lane, _lane, _pin_row, ws_at_pin  # noqa: F401
from tests.test_store_break_attempts import two_member_ws  # noqa: F401


def test_check_set_reads_the_entered_lane_clone(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    res = _invoke("check", "set", str(ws), gr_id, "--", "true")
    out = res.stdout + res.stderr
    assert "head_not_in_member_clone" not in out, out
    assert res.exit_code == 0, out
    assert res.stderr.count("gr2: reading lane north/feature (entered)") == 1, res.stderr
    row = json.loads(res.stdout.strip().splitlines()[0])
    assert row["result"] == "pass" and [m["head"] for m in row["members"]] == [lane_head], row


def test_check_set_with_several_lanes_entered_refuses_by_name(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    _lane(ws, "south")
    res = _invoke("check", "set", str(ws), gr_id, "--", "true")
    out = res.stdout + res.stderr
    assert res.exit_code == 2, out
    assert "north/feature" in out and "south/feature" in out and "gr2 lane exit" in out, out

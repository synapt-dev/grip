"""With several units in a lane, the review verbs refuse by name through the arms they already have.

Before, the refusal was a bare SystemExit: merge and blockers exited 1 with only its text instead of their JSON
receipts, they refused even when every default was given and no lane needed reading, and pin and publish exited 1
with no refusal code. Now merge and blockers with every default given reach the gate as before lanes were read;
blockers that must find the review prints its exit-5 JSON naming both lanes; pin and publish refuse with
`lane_ambiguous`, exit 2.
"""
from __future__ import annotations

import json

from tests.test_review_a4_v2 import _invoke
from tests.test_review_reads_entered_lane import _commit_in_lane, _lane, _pin_row, ws_at_pin  # noqa: F401
from tests.test_store_break_attempts import two_member_ws  # noqa: F401

SEVERAL = "several units have a lane entered"


def _two_lanes(ws):
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    _lane(ws, "south")
    return clone, lane_head


def test_merge_and_blockers_with_every_default_given_reach_the_gate(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone, lane_head = _two_lanes(ws)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    blk = _invoke("review", "blockers", str(ws), gr_id, "--into", "main", "--from", "feat")
    assert SEVERAL not in blk.stdout + blk.stderr, blk.stdout + blk.stderr
    assert blk.exit_code in (0, 3, 5), blk.stdout + blk.stderr
    assert json.loads(blk.stdout.strip().splitlines()[-1])["dry_run"] is True
    merge = _invoke("review", "merge", str(ws), gr_id, "--into", "main", "--from", "feat")
    assert SEVERAL not in merge.stdout + merge.stderr, merge.stdout + merge.stderr
    assert merge.exit_code in (0, 3, 4), merge.stdout + merge.stderr
    assert json.loads(merge.stdout.strip().splitlines()[-1])["id"] == gr_id


def test_blockers_with_no_review_id_prints_exit_5_json_naming_both_lanes(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone, lane_head = _two_lanes(ws)
    _pin_row(ws, remote, base, lane_head, clone)
    blk = _invoke("review", "blockers", str(ws))
    assert blk.exit_code == 5, blk.stdout + blk.stderr
    receipt = json.loads(blk.stdout.strip().splitlines()[-1])
    assert receipt["ready"] is None and SEVERAL in receipt["refused"], receipt
    assert "north/feature" in receipt["refused"] and "south/feature" in receipt["refused"], receipt


def test_publish_and_pin_refuse_lane_ambiguous_exit_2(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone, lane_head = _two_lanes(ws)
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    pub = _invoke("review", "publish", str(ws), gr_id)
    assert pub.exit_code == 2, pub.stdout + pub.stderr
    receipt = json.loads(pub.stdout.strip().splitlines()[-1])
    assert receipt["refused"].startswith("lane_ambiguous: ") and "south/feature" in receipt["refused"], receipt
    pin = _invoke("review", "pin", str(ws))
    assert pin.exit_code == 2, pin.stdout + pin.stderr
    # one plain line on stderr, never a usage panel wrapped at the terminal width
    for result in (pub, pin):
        line = next((ln for ln in result.stderr.splitlines() if "lane_ambiguous: " in ln), "")
        assert "north/feature" in line and "south/feature" in line, result.stderr
        assert "Usage:" not in result.stderr, result.stderr

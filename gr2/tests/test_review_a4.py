"""Tier A4: the review path stops asking for what the workspace and its binds already hold.

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * `review show` omits the range's files, title or body        -> the show rows
  * a bare `review bind` binds a member that is at its pin       -> the inference rows
  * a bare `review bind` binds before printing its rows          -> the order row
  * `review open` with no target picks one of several binds      -> the several-binds row
  * the default lane directory is inside the workspace, is not named, or reuses an existing directory
                                                                  -> the lane-dir rows
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.review_ref_helper import REVIEW_REF_PREFIX
from gr2.python_cli import grip as grip_mod

from tests.test_review_bind_native_store import _bind_args, _unpushed_head
from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401


def _flat(text: str) -> str:
    """Output as a person reads it: colour and the error box's drawing characters removed, wrapped lines joined."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def _bound_ws(two_member_ws: Path) -> tuple[Path, str]:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head), "--title", "the change under review", "--body", "why it matters")
    assert code == 0, out
    return ws, [l for l in out.splitlines() if l.startswith("gr:")][-1]


def test_show_lists_members_range_title_body_and_files_without_the_prefix_strip(two_member_ws: Path) -> None:
    ws, gr_id = _bound_ws(two_member_ws)
    code, out = _cli("review", "show", str(ws), gr_id)
    assert code == 0, out
    assert gr_id in out and "(1 member)" in out and "alpha:" in out
    assert "title: the change under review" in out and "body: why it matters" in out
    assert "files: review-change.txt" in out
    code, raw = _cli("review", "show", str(ws), gr_id[3:])  # a bare sha is accepted too
    assert code == 0 and "review-change.txt" in raw


def test_show_json_carries_the_same_facts(two_member_ws: Path) -> None:
    ws, gr_id = _bound_ws(two_member_ws)
    code, out = _cli("review", "show", str(ws), gr_id, "--json")
    assert code == 0, out
    doc = json.loads(out[out.index("{"):])
    [m] = doc["members"]
    assert doc["id"] == gr_id and m["key"] == "alpha" and m["files"] == ["review-change.txt"]
    assert m["title"].strip() == "the change under review" and len(m["base"]) == 40 and len(m["head"]) == 40


def test_show_refuses_a_commit_that_is_not_a_bind(two_member_ws: Path) -> None:
    ws, _ = _bound_ws(two_member_ws)
    code, out = _cli("review", "show", str(ws), "a" * 40)
    assert code != 0 and "Traceback" not in out


def test_a_bare_bind_binds_the_member_whose_checkout_is_not_at_its_pin(two_member_ws: Path) -> None:
    """THE SMALLEST WORKING PROOF for I14. Only alpha has a commit past its pin; beta is untouched."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    _unpushed_head(ws)
    code, out = _cli("review", "bind", str(ws))
    assert code == 0, out
    assert "gr2: bind alpha " in out and "beta (checkout is at its pin)" in out
    gr_id = [l for l in out.splitlines() if l.startswith("gr:")][-1]
    _, shown = _cli("review", "show", str(ws), gr_id)
    assert "alpha:" in shown and "beta:" not in shown


def test_a_bare_bind_with_every_member_at_its_pin_refuses(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    code, out = _cli("review", "bind", str(ws))
    out = _flat(out)
    assert code != 0 and "nothing to bind" in out and "--rows-json" in out
    assert not [l for l in _git_out(ws, "for-each-ref", REVIEW_REF_PREFIX).splitlines() if l]


def test_members_narrows_and_names_one_that_is_not_a_member(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    _unpushed_head(ws)
    code, out = _cli("review", "bind", str(ws), "--members", "beta")
    assert code != 0 and "nothing to bind" in _flat(out)
    code, out = _cli("review", "bind", str(ws), "--members", "alpha,ghost")
    assert code == 0 and "ghost (not a member of this workspace)" in out


def test_a_member_with_no_pin_is_named_not_bound(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    _unpushed_head(ws)
    toml = ws / "grip.toml"
    lines = toml.read_text().splitlines()
    out_lines, skip_next_pin = [], False
    in_beta = False
    for line in lines:
        if line.strip().startswith("name") and "beta" in line:
            in_beta = True
        elif line.strip().startswith("[[members]]"):
            in_beta = False
        if in_beta and line.strip().startswith("pin"):
            continue
        out_lines.append(line)
    toml.write_text("\n".join(out_lines) + "\n")
    code, out = _cli("review", "bind", str(ws))
    assert code == 0 and "beta (no usable pin recorded)" in out


def test_the_rows_are_printed_before_anything_is_bound(two_member_ws: Path) -> None:
    """Goes red if the rows are announced only after a successful bind: here the bind is REFUSED (behind-must-be-0,
    because the remote moved past the pin) and the chosen row was still printed first, with no ref written."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    _unpushed_head(ws)
    other = ws.parent / "other-clone"
    _git(ws.parent, "clone", "-q", _git_out(ws / "alpha", "remote", "get-url", "origin"), str(other))
    (other / "moved.txt").write_text("the remote moved\n")
    _git(other, "add", ".")
    _git(other, "-c", "user.name=o", "-c", "user.email=o@e.invalid", "commit", "-q", "-m", "moved")
    _git(other, "push", "-q", "origin", "main")
    code, out = _cli("review", "bind", str(ws))
    assert code != 0 and "gr2: bind alpha" in out
    assert not [l for l in _git_out(ws, "for-each-ref", REVIEW_REF_PREFIX).splitlines() if l]


def test_open_with_no_target_opens_the_one_bind_into_a_lane_beside_the_workspace(two_member_ws: Path) -> None:
    ws, gr_id = _bound_ws(two_member_ws)
    code, out = _cli("review", "open", str(ws))
    assert code == 0, out
    lane = ws.parent / f"{ws.name}.review" / gr_id[3:11]
    assert f"gr2: target={gr_id} (the only pinned review in this workspace)" in out
    assert f"gr2: lane-dir={lane}" in out
    assert (lane / ".git").is_dir() and not str(lane).startswith(str(ws) + "/")


def test_open_with_several_binds_lists_them_and_chooses_none(two_member_ws: Path) -> None:
    ws, first = _bound_ws(two_member_ws)
    remote, base, head = _git_out(ws / "alpha", "remote", "get-url", "origin"), _git_out(ws / "alpha", "rev-parse", "origin/main"), _git_out(ws / "alpha", "rev-parse", "HEAD")
    code, out = _cli(*_bind_args(ws, remote, base, head), "--title", "a second bind")
    assert code == 0, out
    second = [l for l in out.splitlines() if l.startswith("gr:")][-1]
    assert second != first
    code, out = _cli("review", "open", str(ws))
    out = _flat(out)
    assert code != 0 and first in out and second in out and "will not choose between them" in out
    assert not (ws.parent / f"{ws.name}.review").exists()


def test_open_with_no_binds_refuses_naming_bind(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    code, out = _cli("review", "open", str(ws))
    out = _flat(out)
    assert code != 0 and "no pinned review to open" in out and "review pin" in out


def test_an_existing_default_lane_dir_is_refused_not_reused(two_member_ws: Path) -> None:
    ws, gr_id = _bound_ws(two_member_ws)
    lane = ws.parent / f"{ws.name}.review" / gr_id[3:11]
    lane.mkdir(parents=True)
    code, out = _cli("review", "open", str(ws), gr_id)
    out = _flat(out)
    assert code != 0 and "already exists" in out and "--lane-dir" in out


def test_a_typed_lane_dir_and_enter_still_work(two_member_ws: Path, tmp_path: Path) -> None:
    ws, gr_id = _bound_ws(two_member_ws)
    lane = tmp_path / "elsewhere"
    code, out = _cli("review", "open", str(ws), gr_id, "--lane-dir", str(lane), "--enter")
    assert code == 0, out
    assert "gr2: lane-dir=" not in out and (lane / ".git").is_dir()


def test_close_removes_what_open_made_and_refuses_a_directory_it_did_not(two_member_ws: Path, tmp_path: Path) -> None:
    ws, gr_id = _bound_ws(two_member_ws)
    assert _cli("review", "open", str(ws))[0] == 0
    lane = ws.parent / f"{ws.name}.review" / gr_id[3:11]
    assert _cli("review", "close", str(lane))[0] == 0 and not lane.exists()
    stranger = tmp_path / "not-a-review"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine")
    code, out = _cli("review", "close", str(stranger))
    assert code != 0 and (stranger / "keep.txt").exists()

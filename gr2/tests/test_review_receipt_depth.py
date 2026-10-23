"""A run receipt nested past a named depth is refused by a scan before the recursive parser reads it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from gr2.python_cli import open_gr_review, review_allocation, review_run
from tests.conftest import make_cli_runner
from tests.test_review_close_keeps_local_work import _multi_member_lane, _text


def test_a_receipt_nested_past_the_limit_is_refused_by_depth_before_the_parser(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    raw = b'{"kind": "review-run", "result": ' + b"[" * 10000 + b"]" * 10000 + b"}"
    (lane / review_run._RECEIPT_NAME).write_bytes(raw)
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert (lane / review_run._RECEIPT_NAME).read_bytes() == raw
    assert (lane / "demo-core" / ".git").exists() and (lane / "demo-web" / ".git").exists()
    alloc = review_allocation.allocation_path(tmp_path / "workspace", lane)
    assert alloc.is_file() and not alloc.with_suffix("").exists(), list(alloc.parent.rglob("*"))
    assert result.exit_code == 2, _text(result)
    assert f"nested deeper than {open_gr_review._RECEIPT_MAX_DEPTH} levels" in _text(result), _text(result)


def _receipt_at_depth(tmp_path: Path, depth: int) -> Path:
    path = tmp_path / "receipt.json"
    path.write_text("[" * depth + "]" * depth)
    return path


def test_depth_64_is_not_refused_for_depth_and_65_is(tmp_path: Path):
    assert open_gr_review._RECEIPT_MAX_DEPTH == 64
    with pytest.raises(open_gr_review.OpenGrReviewError) as at64:
        open_gr_review._read_run_receipt(_receipt_at_depth(tmp_path, 64))
    assert "nested deeper" not in str(at64.value) and "not a JSON object" in str(at64.value)
    with pytest.raises(open_gr_review.OpenGrReviewError) as at65:
        open_gr_review._read_run_receipt(_receipt_at_depth(tmp_path, 65))
    assert "corrupt run receipt: nested deeper than 64 levels; nothing removed, recovery at " in str(at65.value)


def test_brackets_inside_a_string_are_not_nesting(tmp_path: Path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"kind": "review-run", "result": "[" * 1000 + "{" * 1000}))
    assert open_gr_review._read_run_receipt(path)["result"] == "[" * 1000 + "{" * 1000


def test_an_escaped_quote_does_not_end_the_string(tmp_path: Path):
    path = tmp_path / "receipt.json"
    path.write_text('{"kind": "review-run", "result": "a\\"' + "[" * 100 + '"}')
    assert open_gr_review._read_run_receipt(path)["result"] == 'a"' + "[" * 100


def test_multibyte_characters_inside_a_string_do_not_change_the_count(tmp_path: Path):
    """Multi-byte characters and an escaped quote in a string sit before 100 brackets inside that string and
    a 60-deep value outside it: the receipt parses, and the depth counted is the 60 outside the string. (A
    scan of the raw UTF-8 bytes would count the same, since no byte of a multi-byte character is a bracket,
    quote or backslash; the scan reads the decoded text so it sees exactly what json.loads parses.)"""
    path = tmp_path / "receipt.json"
    path.write_bytes(('{"kind": "review-run", "result": "é→\\"' + "[" * 100 + '", "n": '
                      + "[" * 60 + "]" * 60 + "}").encode("utf-8"))
    assert open_gr_review._read_run_receipt(path)["result"] == "é→\"" + "[" * 100
    deep = "é" + "[" * 65 + "]" * 65
    assert open_gr_review._nested_too_deep(deep)
    assert not open_gr_review._nested_too_deep("é" + "[" * 64 + "]" * 64)


def test_a_wide_but_shallow_receipt_is_read_not_refused(tmp_path: Path):
    """The scan counts open containers, not containers seen: 500 sibling lists at depth 2 are 2 deep, not 500."""
    path = tmp_path / "receipt.json"
    receipt = {"kind": "review-run", "result": "green", "notes": [[i] for i in range(500)]}
    path.write_text(json.dumps(receipt))
    assert not open_gr_review._nested_too_deep(path.read_text())
    assert open_gr_review._read_run_receipt(path) == receipt


def test_a_real_run_receipt_still_closes_unchanged(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    receipt = {"kind": "review-run", "created": "2026-10-13T00:00:00+00:00", "result": "green",
               "gr_commit": "abc", "notes": [[1, [2, {"a": [3]}]]]}
    (lane / review_run._RECEIPT_NAME).write_text(json.dumps(receipt) + "\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()

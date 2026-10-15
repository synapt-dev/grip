"""Two crashes become named refusals, in text and under --json: `review repin` on a frozen dir that is
missing or incomplete, and a review id that carries a NUL byte (refused before it reaches the filesystem)."""

from __future__ import annotations

from pathlib import Path

from tests.test_review_verify_json_refusals import _one_refusal, _run
from tests.test_store_break_attempts import two_member_ws  # noqa: F401

NUL_ID = "gr:ab\x00cd"


def _repin(frozen: Path, tmp_path: Path, *extra: str) -> tuple[int, str, str]:
    return _run("review", "repin", str(frozen), "--repo", str(tmp_path), "--out-dir", str(tmp_path / "out"), *extra)


def test_repin_on_a_missing_frozen_dir_is_a_named_text_refusal(tmp_path: Path) -> None:
    code, out, err = _repin(tmp_path / "absent", tmp_path)
    assert (code, out) == (2, ""), out + err
    assert err.startswith("refused: frozen_dir_missing: "), err
    assert "freeze-public-range.sh" in err and "Traceback" not in err, err


def test_repin_on_a_missing_frozen_dir_is_a_json_frozen_dir_missing(tmp_path: Path) -> None:
    code, out, err = _repin(tmp_path / "absent", tmp_path, "--json")
    assert code == 2, out + err
    body = _one_refusal(code, out, "frozen_dir_missing")
    assert str(tmp_path / "absent") in body["message"] and "freeze-public-range.sh" in body["message"], body


def test_repin_on_a_frozen_dir_without_its_range_is_a_json_frozen_dir_incomplete(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    (frozen / "REQUEST.md").write_text("target: x @ " + "a" * 40 + "\n")
    code, out, err = _repin(frozen, tmp_path, "--json")
    assert code == 2, out + err
    assert "range.patch" in _one_refusal(code, out, "frozen_dir_incomplete")["message"]


def test_a_nul_in_the_id_is_a_named_text_refusal(two_member_ws: Path) -> None:
    assert _run("store", "init", str(two_member_ws))[0] == 0
    code, out, err = _run("review", "show", str(two_member_ws), NUL_ID)
    assert (code, out) == (2, ""), out + err
    assert err.startswith("refused: invalid_id: "), err


def test_a_nul_in_the_id_is_a_json_invalid_id_for_every_id_reader(two_member_ws: Path) -> None:
    assert _run("store", "init", str(two_member_ws))[0] == 0
    for verb in ("show", "verify", "open"):
        code, out, err = _run("review", verb, str(two_member_ws), NUL_ID, "--json")
        assert code == 2, (verb, out + err)
        assert "NUL" in _one_refusal(code, out, "invalid_id")["message"], verb

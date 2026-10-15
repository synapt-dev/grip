"""Every review verb with --json refuses as exactly one JSON object on stdout, through one command class
(`json_refusing`), with the exit code the text form gives. One row per verb, each on a refusal path it
printed as text before; the text form is unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_review_verify_json_refusals import _one_refusal, _run
from tests.test_store_break_attempts import two_member_ws  # noqa: F401

SHA = "0" * 40


def test_show_with_no_store_is_a_json_not_initialized(two_member_ws: Path) -> None:
    code, out, err = _run("review", "show", str(two_member_ws), "gr:" + SHA, "--json")
    assert code == 2, out + err
    _one_refusal(code, out, "not_initialized")


def test_open_with_no_store_and_no_id_is_a_json_not_initialized(two_member_ws: Path) -> None:
    code, out, err = _run("review", "open", str(two_member_ws), "--json")
    assert code == 2, out + err
    _one_refusal(code, out, "not_initialized")


def test_open_with_a_lone_pr_number_is_a_json_usage_refusal(two_member_ws: Path) -> None:
    code, out, err = _run("review", "open", str(two_member_ws), "12", "--json")
    assert code == 2, out + err
    assert "OWNER_UNIT REPO PR_NUMBER" in _one_refusal(code, out, "usage")["message"]


def test_show_with_no_workspace_is_a_json_usage_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, err = _run("review", "show", "--json")
    assert code == 2, out + err
    assert "no workspace" in _one_refusal(code, out, "usage")["message"]


def test_check_on_a_workspace_with_no_spec_is_a_json_refusal(two_member_ws: Path) -> None:
    code, out, err = _run("review", "check", str(two_member_ws), "alpha", "1", "--json")
    assert code == 1, out + err
    assert "workspace spec not found" in _one_refusal(code, out, "refused")["message"]


def test_checkout_pr_on_a_workspace_with_no_spec_is_a_json_refusal(two_member_ws: Path) -> None:
    code, out, err = _run("review", "checkout-pr", str(two_member_ws), "unit", "alpha", "1", "--json")
    assert code == 1, out + err
    assert "workspace spec not found" in _one_refusal(code, out, "refused")["message"]


def test_close_outside_any_lane_is_a_json_no_review_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, err = _run("review", "close", "--json")
    assert code == 2, out + err
    _one_refusal(code, out, "no_review_context")


def test_repin_missing_its_required_options_is_a_json_usage_refusal(tmp_path: Path) -> None:
    code, out, err = _run("review", "repin", str(tmp_path), "--json")
    assert code == 2, out + err
    assert "--repo" in _one_refusal(code, out, "usage")["message"]


def test_the_text_forms_are_unchanged(two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = _run("review", "show", str(two_member_ws), "gr:" + SHA)
    assert (code, out) == (2, ""), out + err
    assert err.startswith("not_initialized: "), err
    monkeypatch.chdir(tmp_path)
    code, out, err = _run("review", "close")
    assert (code, out) == (2, ""), out + err
    assert err.startswith("refused: no_review_context: "), err

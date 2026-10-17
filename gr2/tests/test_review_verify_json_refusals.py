"""`review verify --json`: every refusal is exactly one JSON object on stdout, naming the refusal in a
stable `refusal` field, with the same exit code the text form gives. The text form is unchanged."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_review_bind_native_store import _bind_args, _unpushed_head
from tests.test_store_break_attempts import _git, _git_out, app, make_cli_runner, two_member_ws  # noqa: F401


def _run(*args: str) -> tuple[int, str, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout or "", result.stderr or ""


def _one_refusal(code: int, out: str, refusal: str) -> dict:
    """Exactly one JSON object on stdout (json.loads refuses trailing text) that names `refusal`."""
    body = json.loads(out)
    assert isinstance(body, dict), out
    assert body["status"] == "refused" and body["refusal"] == refusal, body
    assert isinstance(body["message"], str) and body["message"], body
    return body


def _bind(ws: Path, title: str) -> str:
    remote, base, head = _unpushed_head(ws) if title == "first" else _another_head(ws)
    code, out, err = _run(*_bind_args(ws, remote, base, head), "--title", title)
    assert code == 0, out + err
    return out.strip().splitlines()[-1]


def _another_head(ws: Path) -> tuple[str, str, str]:
    alpha = ws / "alpha"
    _git(alpha, "commit", "-q", "--allow-empty", "-m", "a second change under review")
    return (_git_out(alpha, "remote", "get-url", "origin"), _git_out(alpha, "rev-parse", "origin/main"),
            _git_out(alpha, "rev-parse", "HEAD"))


def test_nothing_pinned_and_no_id_is_a_json_no_pinned_review(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _run("store", "init", str(ws))[0] == 0
    code, out, err = _run("review", "verify", str(ws), "--json")
    assert code == 2, out + err
    assert _one_refusal(code, out, "no_pinned_review")["message"].startswith("this workspace has no pinned review")
    assert "no pinned review" not in err, err


def test_an_id_that_is_not_pinned_is_a_json_not_bound(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _run("store", "init", str(ws))[0] == 0
    _bind(ws, "first")
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _run("store", "commit", "-m", "root commit")[0] == 0
    root_head = _git_out(ws, "rev-parse", "HEAD")
    code, out, err = _run("review", "verify", str(ws), "gr:" + root_head, "--json")
    assert code == 2, out + err
    _one_refusal(code, out, "not_bound")


def test_two_pinned_and_no_id_is_a_json_refusal_naming_both(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _run("store", "init", str(ws))[0] == 0
    first, second = _bind(ws, "first"), _bind(ws, "second")
    code, out, err = _run("review", "verify", str(ws), "--json")
    assert code == 2, out + err
    body = _one_refusal(code, out, "several_pinned_reviews")
    assert first in body["message"] and second in body["message"], body


def test_no_store_is_a_json_not_initialized(two_member_ws: Path) -> None:
    code, out, err = _run("review", "verify", str(two_member_ws), "--json")
    assert code == 2, out + err
    _one_refusal(code, out, "not_initialized")


def test_no_workspace_at_all_is_a_json_usage_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    code, out, err = _run("review", "verify", "--json")
    assert code == 2, out + err
    assert "no workspace" in _one_refusal(code, out, "usage")["message"]


def test_the_text_form_is_unchanged(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _run("store", "init", str(ws))[0] == 0
    _bind(ws, "first")
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _run("store", "commit", "-m", "root commit")[0] == 0
    code, out, err = _run("review", "verify", str(ws), "gr:" + _git_out(ws, "rev-parse", "HEAD"))
    assert (code, out) == (2, ""), out + err
    assert err.startswith("not_bound: No review "), err

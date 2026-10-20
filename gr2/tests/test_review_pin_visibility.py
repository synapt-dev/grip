"""`gr2 review pin --visibility NAME=public|private` declares a member's visibility on the ordinary (no-row) path.

Publish refuses a review of several members unless each one declares its visibility, and before this flag only a
--rows-json row could carry it. Each row names what must make it go red:
  * the flag is parsed but not applied to the rows the no-row path builds   -> the publish row
  * the publish refusal stops naming the flag                                -> the refusal row
  * an unknown member name or value is pinned anyway                         -> the two refusal-at-pin rows
"""
from __future__ import annotations

from pathlib import Path

from tests.test_review_pin_no_pin import _feature, _flat, _gr_id, _no_review_ref, _remote_url, _spec_ws
from tests.test_store_break_attempts import _cli, two_member_ws  # noqa: F401


def _review_on(remote: str, full: str) -> bool:
    import subprocess

    out = subprocess.run(["git", "-C", remote, "for-each-ref", "--format=%(refname)"], capture_output=True, text=True)
    return full in out.stdout


def test_a_two_member_no_row_pin_declared_public_publishes_to_a_members_remote(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    code, out = _cli("review", "pin", str(ws), "--visibility", "alpha=public", "--visibility", "beta=public")
    assert code == 0, out
    gr_id = _gr_id(out)
    remote = _remote_url(ws / "alpha").removeprefix("file://")
    code, out = _cli("review", "publish", str(ws), gr_id, "--remote", remote)
    assert code == 0, out
    assert _review_on(remote, gr_id[3:]), out


def test_the_same_pin_without_the_flag_refuses_at_publish_naming_the_flag(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    code, out = _cli("review", "pin", str(ws))
    assert code == 0, out
    remote = _remote_url(ws / "alpha").removeprefix("file://")
    code, out = _cli("review", "publish", str(ws), _gr_id(out), "--remote", remote)
    text = _flat(out)
    assert code == 2, out
    assert "visibility_mismatch" in text and "--visibility alpha=public|private" in text, text
    assert "--visibility beta=public|private" in text and "gr2 review pin" in text, text


def test_an_unknown_member_name_refuses_at_pin_and_nothing_is_pinned(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    code, out = _cli("review", "pin", str(ws), "--visibility", "alpha=public", "--visibility", "gamma=public")
    text = _flat(out)
    assert code == 2 and "Traceback" not in out, out
    assert "'gamma'" in text and "not a member being pinned" in text, text
    assert _no_review_ref(ws)


def test_an_unknown_visibility_value_refuses_at_pin_and_nothing_is_pinned(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    code, out = _cli("review", "pin", str(ws), "--visibility", "alpha=public", "--visibility", "beta=secret")
    text = _flat(out)
    assert code == 2 and "Traceback" not in out, out
    assert "must be public or private" in text and "'secret'" in text, text
    assert _no_review_ref(ws)

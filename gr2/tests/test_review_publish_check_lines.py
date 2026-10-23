"""Every refusal a `review publish` raises says what to do: a printed next, or a check line where no command is safe."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from gr2.python_cli.commit import LaneAmbiguous
from gr2.python_cli.next_steps import PUBLISH_REFUSED_CHECK, PUBLISH_REFUSED_NEXT
from tests.test_review_a4_v2 import _invoke
from tests.test_review_pin_no_pin import _feature, _gr_id, _spec_ws
from tests.test_review_reads_entered_lane import _commit_in_lane, _lane, _pin_row, ws_at_pin  # noqa: F401
from tests.test_store_break_attempts import _cli, two_member_ws  # noqa: F401

GRIP = Path(__file__).resolve().parents[1] / "gr2" / "python_cli" / "grip.py"
# Its detail already names the fix (publish to a local destination); a check line for it is not yet written.
NOT_YET_CHECKED = {"local_path_remote"}


def _publish_codes() -> set[str]:
    """The codes raised from the member preflight through the bare publish, the functions a publish runs."""
    text = GRIP.read_text()
    span = text[text.index("\ndef _member_preflight("):text.index("\ndef receive_review_commit(")]
    return set(re.findall(r'GripReviewRefused\("([a-z0-9_]+)"', span)) | {LaneAmbiguous.refusal}


def _check_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("check: ")]
    assert len(lines) == 1, out
    return lines[0]


def _host_local_bare(tmp_path: Path) -> str:
    """A host-local remote that is no member's: publish reads it as private."""
    bare = tmp_path / "host-local.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return str(bare)


def test_visibility_mismatch_prints_a_check(two_member_ws) -> None:
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    _code, out = _cli("review", "pin", str(ws))
    gr_id = _gr_id(out)
    code, out = _cli("review", "publish", str(ws), gr_id)
    assert code == 2 and "visibility_mismatch" in out
    assert "must declare one (gr2 review pin --visibility" in _check_line(out), out
    assert not any(ln.startswith("next: ") for ln in out.splitlines()), out


def test_an_undeclared_member_refuses_even_to_a_host_local_remote(two_member_ws, tmp_path) -> None:
    """The check line's first clause: a host-local target does not get past a member with no declared visibility."""
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    _code, out = _cli("review", "pin", str(ws))
    code, out = _cli("review", "publish", str(ws), _gr_id(out), "--remote", _host_local_bare(tmp_path))
    assert code == 2 and "visibility_mismatch" in out, out


def test_a_private_member_publishes_only_to_a_host_local_remote(two_member_ws, tmp_path) -> None:
    """The check line's second clause: with every member declared, a private member refuses a public target and goes
    to a host-local one."""
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    code, out = _cli("review", "pin", str(ws), "--visibility", "alpha=private", "--visibility", "beta=public")
    assert code == 0, out
    gr_id = _gr_id(out)
    code, out = _cli("review", "publish", str(ws), gr_id)
    assert code == 2 and "visibility_mismatch" in out, out
    assert "(gr2 review publish --remote <path>)" in _check_line(out), out
    code, out = _cli("review", "publish", str(ws), gr_id, "--remote", _host_local_bare(tmp_path))
    assert code == 0, out


def test_lane_ambiguous_prints_a_check(ws_at_pin) -> None:
    ws, remote, base, _head = ws_at_pin
    clone = _lane(ws, "north")
    lane_head = _commit_in_lane(ws, clone)
    _lane(ws, "south")
    gr_id = _pin_row(ws, remote, base, lane_head, clone)
    pub = _invoke("review", "publish", str(ws), gr_id)
    assert pub.exit_code == 2 and "lane_ambiguous: " in pub.stderr
    assert any(ln.startswith("check: ") for ln in pub.stderr.splitlines()), pub.stderr


def test_every_publish_refusal_has_a_check_or_a_next() -> None:
    codes = _publish_codes()
    assert "visibility_mismatch" in codes and "feature_moved" in codes, codes
    bare = {c for c in codes - NOT_YET_CHECKED if c not in PUBLISH_REFUSED_CHECK and not PUBLISH_REFUSED_NEXT.get(c)}
    assert not bare, bare


def test_every_none_next_has_a_check() -> None:
    bare = {c for c, build in PUBLISH_REFUSED_NEXT.items() if build is None and c not in PUBLISH_REFUSED_CHECK}
    assert not bare, bare

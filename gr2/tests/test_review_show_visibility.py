"""`review show` prints each member's declared visibility and the one rule bare publish runs on it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from gr2.python_cli import next_steps
from tests.test_review_pin_no_pin import _feature, _gr_id, _spec_ws
from tests.test_store_break_attempts import _cli, two_member_ws  # noqa: F401


def _pinned(ws: Path, *flags: str) -> str:
    code, out = _cli("review", "pin", str(ws), *flags)
    assert code == 0, out
    return _gr_id(out)


def _two(two_member_ws: Path) -> tuple[Path, str]:
    ws = _spec_ws(two_member_ws)
    for n in ("alpha", "beta"):
        _feature(ws / n)
    return ws, _pinned(ws, "--visibility", "alpha=public", "--visibility", "beta=private")


def test_r1_show_prints_each_members_visibility_and_the_publish_line(two_member_ws: Path) -> None:
    ws, gr_id = _two(two_member_ws)
    code, out = _cli("review", "show", str(ws), gr_id)
    assert code == 0, out
    assert out.split("alpha:", 1)[1].split("beta:", 1)[0].count("  visibility: public\n") == 1, out
    assert out.split("beta:", 1)[1].count("  visibility: private\n") == 1, out
    assert "bare publish visibility: refuses visibility_mismatch: 'beta' is private" in out, out
    code, out = _cli("review", "show", str(ws), gr_id, "--json")
    view = json.loads(out)
    assert {m["key"]: m["visibility"] for m in view["members"]} == {"alpha": "public", "beta": "private"}
    assert view["publish_visibility"]["ok"] is False
    assert view["publish_visibility"]["refusal"].startswith("visibility_mismatch: 'beta' is private")


def test_r2_show_line_agrees_with_bare_publish(two_member_ws: Path) -> None:
    ws, gr_id = _two(two_member_ws)
    code, out = _cli("review", "publish", str(ws), gr_id)
    assert code != 0 and "visibility_mismatch" in out, out
    code, shown = _cli("review", "show", str(ws), gr_id)
    assert "bare publish visibility: refuses visibility_mismatch" in shown, shown


def test_r3_check_line_points_at_show() -> None:
    line = next_steps.PUBLISH_REFUSED_CHECK["visibility_mismatch"]
    assert line.startswith("check: see each member's visibility with gr2 review show <workspace> <review>; "), line


def test_r4_json_is_additive_over_dev(two_member_ws: Path) -> None:
    """Every key dev's --json gives, at every level, is still there with the same value (dev built in the row)."""
    ws, gr_id = _two(two_member_ws)
    dev = os.environ.get("GR2_DEV_TREE")
    if dev:
        env = {**os.environ, "PYTHONPATH": dev}
        proc = subprocess.run([sys.executable, "-c", "from gr2.python_cli.app import app; app()",
                               "review", "show", str(ws), gr_id, "--json"], capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr
        old = json.loads(proc.stdout)
    else:  # dev's view, rebuilt in-process: the same call show made before the change
        from gr2.python_cli import grip
        old = json.loads(json.dumps(grip.show_review_commit(ws, gr_id.removeprefix("gr:")), default=str))
    new = json.loads(_cli("review", "show", str(ws), gr_id, "--json")[1])
    for key, value in old.items():
        if key == "members":
            continue
        assert new[key] == value, key
    for before, after in zip(old["members"], new["members"], strict=True):
        for key, value in before.items():
            if key != "files":
                assert after[key] == value, key


def test_r5_single_undeclared_member_is_exempt(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    _feature(ws / "alpha")
    gr_id = _pinned(ws)
    code, out = _cli("review", "show", str(ws), gr_id)
    assert code == 0, out
    assert out.count("(1 member)") == 1, out
    assert out.count("  visibility: undeclared\n") == 1, out
    assert out.count("bare publish visibility: ok\n") == 1, out
    view = json.loads(_cli("review", "show", str(ws), gr_id, "--json")[1])
    assert view["publish_visibility"] == {"ok": True, "refusal": None}

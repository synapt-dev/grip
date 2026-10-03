"""`review run` reads its install order through the ecosystem plugins, not through a Python-only reader.

`test_review_run_dependency_order.py` is the conformance baseline: every one of its rows runs unchanged against
the plugin-backed derivation. These rows are what the baseline cannot show, because a Python lane never needs a
plugin: that a plugin on the USER's PATH is asked and its answer orders the lane, that the PATH is what is read,
that a plugin which fails refuses the lane instead of falling back to the marker order, and that a plugin never
comes from a member.

Each row names what must make it go red:
  * the derivation ignores plugins              -> the claim row
  * the plugin table is not built from PATH     -> the PATH row
  * a plugin failure falls back to marker order -> the failure row
  * a member directory is searched for plugins  -> the member-plugin row
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from gr2.python_cli import review_run as rr

pytestmark_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="external plugins are executables with a shebang line; POSIX only"
)

FAKE_ANSWER = (
    "import json, sys\n"
    "req = json.load(sys.stdin)\n"
    "if sys.argv[1] == 'describe':\n"
    "    key = req['key']\n"
    "    # b is needed by a: a must install after b, though the marker order is [a, b]\n"
    "    edges = [{'to': 'fake:b', 'kind': 'install', 'via': 'a needs b'}] if key == 'a' else []\n"
    "    print(json.dumps({'protocol': 1, 'ok': True, 'units': [{'id': 'fake:' + key, 'dir': req['dir'], 'edges': edges}]}))\n"
    "else:\n"
    "    print(json.dumps({'protocol': 1, 'ok': True, 'method': 'm'}))\n"
)


def _lane(tmp_path: Path, *keys: str) -> Path:
    lane = tmp_path / "lane"
    for key in keys:
        (lane / key).mkdir(parents=True)
    return lane


def _plugin_dir(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / f"grip-ecosystem-{name}"
    exe.write_text(f"#!{sys.executable}\n{body}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _fake(call: str, request: dict) -> dict:
    """The same plugin as FAKE_ANSWER, in process."""
    if call == "describe":
        key = request["key"]
        edges = [{"to": "fake:b", "kind": "install", "via": "a needs b"}] if key == "a" else []
        return {"protocol": 1, "ok": True, "units": [{"id": f"fake:{key}", "dir": request["dir"], "edges": edges}]}
    return {"protocol": 1, "ok": True, "method": "m"}


def test_a_plugin_claim_orders_the_lane_where_no_python_metadata_exists(tmp_path: Path) -> None:
    """Neither member has a pyproject, so the built-in Python plugin claims nothing and the old reader would
    return the marker order [a, b]. The plugin says a needs b, so b installs first. Goes red if the derivation
    does not ask the plugins it is given."""
    lane = _lane(tmp_path, "a", "b")
    table = {"fake": _fake}
    assert rr._derive_member_order(lane, ["a", "b"], plugins=table) == ["b", "a"]
    assert rr._derive_member_order(lane, ["b", "a"], plugins=table) == ["b", "a"]  # the other marker order, same answer


@pytestmark_posix
def test_the_plugins_come_from_the_users_path(tmp_path: Path, monkeypatch) -> None:
    """The default plugin table is built from PATH, which is the user's, never from the lane. Goes red if the
    table is built from an empty path (the order comes back as the marker order)."""
    lane = _lane(tmp_path, "a", "b")
    bin_dir = _plugin_dir(tmp_path, "fake", FAKE_ANSWER)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert rr._derive_member_order(lane, ["a", "b"]) == ["b", "a"]
    monkeypatch.setenv("PATH", os.environ["PATH"].replace(str(bin_dir), ""))
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"], "control: without the plugin on PATH, marker order"


@pytestmark_posix
def test_a_plugin_that_fails_refuses_the_lane_and_does_not_fall_back_to_marker_order(tmp_path: Path, monkeypatch) -> None:
    """A plugin that exits non-zero must refuse the lane as plugin_failure. Falling back to the marker order
    would install a lane in an order nobody derived and call it derived."""
    lane = _lane(tmp_path, "a", "b")
    bin_dir = _plugin_dir(tmp_path, "broken", "import sys\nsys.exit(3)\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr._derive_member_order(lane, ["a", "b"])
    assert exc.value.code == "plugin_failure" and "broken" in exc.value.detail and "exit status 3" in exc.value.detail


@pytestmark_posix
def test_a_plugin_inside_a_member_is_never_run(tmp_path: Path, monkeypatch) -> None:
    """The change under review must not choose its own install order: an executable named like a plugin that a
    MEMBER carries is not discovered, because only PATH is searched. Goes red if the lane's directories are
    searched. The member's executable would reverse the order if it were run."""
    lane = _lane(tmp_path, "a", "b")
    _plugin_dir(lane / "a", "evil", FAKE_ANSWER)  # <lane>/a/bin/grip-ecosystem-evil
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    assert rr._derive_member_order(lane, ["a", "b"]) == ["a", "b"]

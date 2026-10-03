"""A review lane RUNS its downstream and upstream members, at their pins, and the receipt says what ran.

Written by the author of the change (not by an independent reader). The changed members are the witness lane's
`a-web` (distribution web-app) and `z-core` (core-lib); the other members are real git remotes whose pin
differs from their tip. Each row names what must make it go red:
  * downstream members are not run                  -> the tested-together row
  * upstream members are tested                     -> the upstream row (its test would fail)
  * a red downstream member stops the run / is hidden -> the red row
  * a pinned member is not integrity-checked        -> the tamper row
  * a requested downstream is left out of a receipt -> the not_examined / skipped / refused rows
  * the CLI verdict does not say what was tested    -> the CLI rows
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from gr2.python_cli import review_run as rr

from tests.test_lane_downstream import _g, _lane_with_workspace, _workspace
from tests.test_review_run_dependency_order import _declare, _witness_lane
from tests.test_review_run_multi_repo import PTH_SCRIPT, _cli, _member

API_SRC = "def handle(q):\n    from demo_core import total\n    return total(q)\n"


def _api_test(core_dir: Path, *, red: bool = False) -> str:
    return (
        "import demo_core\nfrom demo_api import handle\n\n"
        f"def test_handle():\n    assert handle(4) == {1 if red else 1000}\n\n"
        "def test_core_is_the_reviewed_one():\n"
        f"    assert demo_core.__file__.startswith({str(core_dir)!r}), demo_core.__file__\n"
    )


def _remote(tmp_path: Path, key: str, pkg: str, dist: str, deps: list[str], src: str, test: str) -> tuple[Path, str]:
    """A member repo whose PIN is the commit that declares `deps`; a later commit (the tip) adds a file."""
    root = tmp_path / "remotes"
    root.mkdir(exist_ok=True)
    _member(root, key, pkg, src, test)
    _declare(root, key, dist, deps)
    pin = _g(root / key, "rev-parse", "HEAD")
    (root / key / "tip-only").write_text("the tip, not the pin")
    _g(root / key, "add", "-A")
    _g(root / key, "commit", "-qm", "tip")
    return root / key, pin


def _entry(name: str, repo: Path, pin: str) -> dict:
    return {"name": name, "path": name, "url": str(repo), "pin": pin}


def test_a_downstream_member_runs_at_its_pin_against_the_reviewed_code(tmp_path: Path) -> None:
    """`api` needs core-lib and its test asserts it imported the lane's CHANGED z-core. Goes red if downstream
    members are not run, if they run at the tip (the file `tip-only` would exist), or if the receipt does not
    name them."""
    lane = tmp_path / "lane"  # _witness_lane makes this directory; the remote's test needs its path now
    api, pin = _remote(tmp_path, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, _api_test(lane / "z-core"))
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [_entry("api", api, pin)])
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "green", receipt
    roles = {m["key"]: m["role"] for m in receipt["members"]}
    assert roles == {"a-web": "changed", "z-core": "changed", "api": "downstream"}
    api_entry = next(m for m in receipt["members"] if m["key"] == "api")
    assert api_entry["tested"] is True and api_entry["passed"] >= 2 and api_entry["bound_head"] == pin
    assert receipt["order"].index("z-core") < receipt["order"].index("api")
    assert receipt["downstream"]["status"] == "ran"
    assert receipt["downstream"]["downstream"] == ["api"]
    assert sorted(receipt["downstream"]["tested"]) == ["a-web", "api", "z-core"]
    assert not (lane / "api" / "tip-only").exists(), "the member ran at the remote's tip, not its pin"
    assert receipt["selected"] == sum(m["selected"] for m in receipt["members"])


def test_an_upstream_member_is_installed_and_never_tested(tmp_path: Path) -> None:
    """core-lib needs base-lib, so base is installed first, and its test (which would FAIL) is never run. Goes red
    if upstream members are tested: the lane would be red."""
    base, pin = _remote(tmp_path, "base", "demo_base", "base-lib", [], "VALUE = 1\n",
                        "def test_never_run():\n    assert False\n")
    lane = _witness_lane(tmp_path, core_deps=["base-lib"])
    ws = _workspace(tmp_path, [_entry("base", base, pin)])
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    marker["workspace_root"] = str(ws)
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker))
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "green", receipt
    assert receipt["order"][0] == "base"
    base_entry = receipt["members"][0]
    assert base_entry["role"] == "upstream" and base_entry["tested"] is False and base_entry["result"] == "installed"
    assert base_entry["passed"] == 0 and base_entry["selected"] == 0
    assert receipt["downstream"]["upstream"] == ["base"] and "base" not in receipt["downstream"]["tested"]


def test_a_red_downstream_member_makes_the_lane_red_and_does_not_hide_the_others(tmp_path: Path) -> None:
    lane = tmp_path / "lane"
    api, pin = _remote(tmp_path, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC,
                       _api_test(lane / "z-core", red=True))
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [_entry("api", api, pin)])
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "red"
    by_key = {m["key"]: m for m in receipt["members"]}
    assert by_key["api"]["result"] == "red" and by_key["api"]["failed"] >= 1
    assert by_key["a-web"]["result"] == "green" and by_key["z-core"]["result"] == "green"
    assert receipt["failed"] >= 1 and by_key["api"]["failed_ids"]


def test_a_pinned_member_that_rewrites_itself_during_install_is_refused_by_name(tmp_path: Path) -> None:
    """The same integrity as a bound member, with the pin's tree as the bound tree. Goes red if pinned members
    are left out of the intact check."""
    lane = tmp_path / "lane"
    api, pin0 = _remote(tmp_path, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, _api_test(lane / "z-core"))
    tamper = PTH_SCRIPT + ";open('src/demo_api/__init__.py','a').write('# rewritten by the install')"
    import shlex
    hint = "install = " + " ".join(shlex.quote(t) for t in ["{venv}", "-c", tamper, "api", "{lane}/src"]) + "\npackage = demo_api\n"
    (api / ".review-install").write_text(hint)
    _g(api, "add", "-A")
    _g(api, "commit", "-qm", "install that rewrites the member")
    pin = _g(api, "rev-parse", "HEAD")
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [_entry("api", api, pin)])
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "tree_drift" and exc.value.member == "api", (exc.value.code, exc.value.member)
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused" and receipt["downstream"]["status"] in {"selected", "not_examined", "ran"}


def test_the_receipt_always_says_what_downstream_did(tmp_path: Path) -> None:
    """Not examined (no workspace), skipped (the flag), skipped (an explicit --order), refused (an unpinned
    member): four ways to not test downstream, and none of them leaves the receipt silent."""
    (tmp_path / "plain").mkdir()
    lane = _witness_lane(tmp_path / "plain")
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["downstream"]["status"] == "not_examined" and "no workspace" in receipt["downstream"]["reason"]

    (tmp_path / "flag").mkdir()
    lane = _witness_lane(tmp_path / "flag")
    receipt = rr.run_review_lane(lane, pytest_args=["-q"], downstream=False)
    assert receipt["downstream"]["status"] == "skipped" and "--no-downstream" in receipt["downstream"]["reason"]

    (tmp_path / "order").mkdir()
    lane = _witness_lane(tmp_path / "order")
    receipt = rr.run_review_lane(lane, pytest_args=["-q"], order=["z-core", "a-web"])
    assert receipt["downstream"]["status"] == "skipped" and "--order" in receipt["downstream"]["reason"]


def test_an_unpinned_member_refusal_receipt_carries_the_downstream_record(tmp_path: Path) -> None:
    root = tmp_path / "x"
    root.mkdir()
    repo, pin = _remote(root, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, "def test_x():\n    pass\n")
    lane, _ = _lane_with_workspace(root, lambda r: [{"name": "api", "path": "api", "url": str(repo)}])
    with pytest.raises(rr.ReviewRunRefused):
        rr.run_review_lane(lane, pytest_args=["-q"])
    block = json.loads((lane / rr._RECEIPT_NAME).read_text())["downstream"]
    assert block["status"] == "refused" and block["code"] == "downstream_unpinned" and "'api'" in block["reason"]


def test_the_default_on_refusal_names_the_way_out(tmp_path: Path) -> None:
    """Downstream is the default, so the refusal for an unpinned member is the first thing a stranger meets and
    it must say how to review the lane's own members only. Goes red if the message does not name the flag."""
    root = tmp_path / "y"
    root.mkdir()
    repo, _ = _remote(root, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, "def test_x():\n    pass\n")
    lane, _ = _lane_with_workspace(root, lambda r: [{"name": "api", "path": "api", "url": str(repo)}])
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "downstream_unpinned"
    assert "--no-downstream" in exc.value.detail and "downstream: skipped" in exc.value.detail
    code, out = _cli("review", "run", str(lane))
    assert code == 2 and "--no-downstream" in out, out
    code, out = _cli("review", "run", str(lane), "--no-downstream")
    assert code == 0 and "downstream: skipped" in out, out


def test_the_cli_says_what_was_tested_together_and_the_flag_says_what_was_not(tmp_path: Path) -> None:
    lane = tmp_path / "lane"
    api, pin = _remote(tmp_path, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, _api_test(lane / "z-core"))
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [_entry("api", api, pin)])
    code, out = _cli("review", "run", str(lane))
    assert code == 0, out
    assert "api: green (downstream)" in out
    assert "downstream: ran (changed z-core, a-web; downstream api)" in out

    # a second lane for the flag (the first lane's directories are used)
    second = tmp_path / "second"
    second.mkdir()
    api2, pin2 = _remote(second, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, "def test_x():\n    pass\n")
    lane2, _ = _lane_with_workspace(second, lambda root: [_entry("api", api2, pin2)])
    code, out = _cli("review", "run", str(lane2), "--no-downstream")
    assert code == 0, out
    assert "downstream: skipped (--no-downstream" in out and "api:" not in out

"""A root made by `store init` alone records its pins in `grip.toml` `[[members]]`, not in the workspace spec;
downstream reads them from there, from the spec, or from both, and every pin is judged by ONE validator.

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * grip.toml is not read when the spec is absent       -> the store-path lane row
  * grip.toml pins skip the 40-hex check                -> the bad-pin row
  * two disagreeing pins are resolved silently           -> the conflict rows
  * a member with no url in grip.toml raises KeyError    -> the no-url row
  * a single-repo receipt says nothing about downstream  -> the single-repo rows
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from gr2.python_cli import lane_downstream as ld
from gr2.python_cli import review_run as rr

from tests.test_lane_downstream import _g
from tests.test_lane_downstream_run import API_SRC, _api_test, _remote
from tests.test_review_run_dependency_order import _witness_lane
from tests.test_review_run_multi_repo import _cli
from tests import test_review_run as single


def _write_grip_toml(root: Path, members: list[dict]) -> None:
    """`members`: dicts of name, optional pin, optional url (written under [members.remotes] origin)."""
    root.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version = 1", f'workspace_name = "{root.name}"', ""]
    for m in members:
        lines += ["[[members]]", f'name = "{m["name"]}"', f'path = "{m["name"]}"', 'ref = "main"']
        if "pin" in m:
            lines.append(f'pin = "{m["pin"]}"')
        lines.append('mode = "full"')
        if "url" in m:
            lines += ["[members.remotes]", f'origin = "{m["url"]}"']
        lines.append("")
    (root / "grip.toml").write_text("\n".join(lines))


def _write_spec(root: Path, repos: list[dict]) -> None:
    (root / ".grip").mkdir(parents=True, exist_ok=True)
    lines = [f'workspace_name = "{root.name}"', ""]
    for r in repos:
        lines += ["[[repos]]", f'name = "{r["name"]}"', f'path = "{r["name"]}"']
        if "url" in r:
            lines.append(f'url = "{r["url"]}"')
        if "pin" in r:
            lines.append(f'pin = "{r["pin"]}"')
        lines.append("")
    (root / ".grip" / "workspace_spec.toml").write_text("\n".join(lines))


def _point_lane_at(lane: Path, root: Path) -> None:
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    marker["workspace_root"] = str(root)
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker))


A, B = "a" * 40, "b" * 40


# --- load_members: the one reader ------------------------------------------------------------------------


def test_each_source_reads_as_itself_and_neither_reads_as_none(tmp_path: Path) -> None:
    only_toml = tmp_path / "t"
    _write_grip_toml(only_toml, [{"name": "api", "pin": A, "url": "/r/api"}, {"name": "nourl", "pin": B}])
    assert ld.load_members(only_toml) == [
        {"name": "api", "url": "/r/api", "pin": A, "path": "api", "ref": "main"},
        {"name": "nourl", "url": None, "pin": B, "path": "nourl", "ref": "main"},
    ]
    only_spec = tmp_path / "s"
    _write_spec(only_spec, [{"name": "api", "url": "/r/api", "pin": A}])
    assert ld.load_members(only_spec) == [{"name": "api", "url": "/r/api", "pin": A, "path": "api", "ref": None}]
    (tmp_path / "none").mkdir()
    assert ld.load_members(tmp_path / "none") is None


def test_both_sources_are_matched_by_name_and_never_resolved_silently(tmp_path: Path) -> None:
    root = tmp_path / "both"
    _write_spec(root, [{"name": "api", "url": "/r/api", "pin": A}, {"name": "x", "url": "/r/x"}])
    _write_grip_toml(root, [{"name": "api", "pin": A}, {"name": "x", "pin": B, "url": "/r/x2"}, {"name": "only", "pin": A, "url": "/r/o"}])
    got = {m["name"]: m for m in ld.load_members(root)}
    assert got["api"]["pin"] == A and got["api"]["url"] == "/r/api"          # agree
    assert got["x"]["pin"] == B and got["x"]["url"] == "/r/x"                # a usable pin in one file only is used
    assert got["only"]["pin"] == A                                           # a member only in grip.toml is added
    _write_grip_toml(root, [{"name": "api", "pin": B, "url": "/r/api"}])
    with pytest.raises(ld.PinConflict) as exc:
        ld.load_members(root)
    text = str(exc.value)
    assert exc.value.member == "api" and A[:12] in text and B[:12] in text and "grip.toml" in text and "workspace_spec.toml" in text


@pytest.mark.parametrize("pin", ["main", "abc1234", "A" * 40, "g" * 40, ""])
def test_a_grip_toml_pin_that_is_not_a_full_lowercase_commit_is_not_a_pin(tmp_path: Path, pin: str) -> None:
    """The new source goes through the same validator. Goes red if grip.toml pins skip pin_of."""
    root = tmp_path / "t"
    _write_grip_toml(root, [{"name": "api", "pin": pin, "url": "/r/api"}])
    [member] = ld.load_members(root)
    assert ld.pin_of(member) is None
    with pytest.raises(ld.PinRefused):
        ld.materialize_at_pin(member, tmp_path / "lane" / "api", workspace_root=root)


def test_a_grip_toml_member_with_a_pin_and_no_url_is_a_refusal_not_a_keyerror(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write_grip_toml(root, [{"name": "api", "pin": A}])
    [member] = ld.load_members(root)
    with pytest.raises(ld.PinRefused) as exc:
        ld.materialize_at_pin(member, tmp_path / "lane" / "api", workspace_root=root)
    assert "no url" in exc.value.reason


# --- the lane: a store-path-only root runs downstream at the pin -----------------------------------------


def _store_path_lane(tmp_path: Path, *, pin: str | None) -> tuple[Path, Path, str]:
    lane = tmp_path / "lane"
    api, api_pin = _remote(tmp_path, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, _api_test(lane / "z-core"))
    lane = _witness_lane(tmp_path)
    root = tmp_path / "root"
    member = {"name": "api", "url": str(api)}
    if pin is not None:
        member["pin"] = pin if pin != "pin" else api_pin
    _write_grip_toml(root, [member])
    _point_lane_at(lane, root)
    return lane, root, api_pin


def test_a_lane_on_a_store_path_only_root_runs_downstream_at_the_pin(tmp_path: Path) -> None:
    """THE SMALLEST WORKING PROOF. The root has grip.toml and no `.grip/workspace_spec.toml`. Goes red if grip.toml
    is not read (the receipt would say not_examined), or if the member runs at the remote's tip."""
    lane, root, api_pin = _store_path_lane(tmp_path, pin="pin")
    assert not (root / ".grip").exists()
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "green", receipt
    assert receipt["downstream"]["status"] == "ran" and receipt["downstream"]["downstream"] == ["api"]
    api_entry = next(m for m in receipt["members"] if m["key"] == "api")
    assert api_entry["role"] == "downstream" and api_entry["bound_head"] == api_pin and api_entry["passed"] >= 2
    assert not (lane / "api" / "tip-only").exists()


def test_the_same_root_with_no_pin_refuses_naming_the_way_out(tmp_path: Path) -> None:
    lane, root, _ = _store_path_lane(tmp_path, pin=None)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "downstream_unpinned" and "no usable pin" in exc.value.detail
    assert "--no-downstream" in exc.value.detail
    assert not (lane / rr._VENV_DIRNAME).exists()


def test_a_branch_name_in_grip_toml_refuses_as_unpinned_not_as_a_pin(tmp_path: Path) -> None:
    lane, root, _ = _store_path_lane(tmp_path, pin="main")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "downstream_unpinned" and not (lane / "api").exists()


def test_two_files_that_disagree_refuse_the_lane_naming_both_pins(tmp_path: Path) -> None:
    lane, root, api_pin = _store_path_lane(tmp_path, pin="pin")
    _write_spec(root, [{"name": "api", "url": str(root), "pin": "c" * 40}])
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "downstream_pin_conflict"
    assert api_pin[:12] in exc.value.detail and ("c" * 12) in exc.value.detail
    assert not (lane / rr._VENV_DIRNAME).exists() and not (lane / "api").exists()
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["refusal_code"] == "downstream_pin_conflict"


def test_the_workspace_spec_path_is_unchanged_when_only_the_spec_exists(tmp_path: Path) -> None:
    lane = tmp_path / "lane"
    api, api_pin = _remote(tmp_path, "api", "demo_api", "api-lib", ["core-lib>=1"], API_SRC, _api_test(lane / "z-core"))
    lane = _witness_lane(tmp_path)
    root = tmp_path / "ws"
    _write_spec(root, [{"name": "api", "url": str(api), "pin": api_pin}])
    _point_lane_at(lane, root)
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["downstream"]["status"] == "ran" and receipt["result"] == "green"


# --- the single-repo lane says what it did not do --------------------------------------------------------


def test_a_single_repo_lane_receipt_says_downstream_was_not_examined(tmp_path: Path) -> None:
    lane, tree = single._pkg_repo(tmp_path, test_body=single.PASS_TEST)
    single._write_marker(lane, _g(lane, "rev-parse", "HEAD"), tree)
    receipt = rr.run_review_lane(
        lane, package="demo_pkg", pytest_args=[], install=single._offline_install(lane)
    )
    assert receipt["downstream"] == {
        "status": "not_examined",
        "reason": "a single-repo lane; downstream is examined for multi-member lanes in this version",
    }
    skipped = rr._single_repo_downstream(False)
    assert skipped["status"] == "skipped" and "--no-downstream" in skipped["reason"]


def test_the_single_repo_verdict_line_says_downstream_was_not_examined(tmp_path: Path) -> None:
    import shlex

    lane, tree = single._pkg_repo(tmp_path, test_body=single.PASS_TEST)
    single._write_marker(lane, _g(lane, "rev-parse", "HEAD"), tree)
    code, out = _cli("review", "run", str(lane), "--package", "demo_pkg", "--install",
                     shlex.join(single._offline_install(lane)))
    assert code == 0, out
    assert "downstream: not examined (a single-repo lane; downstream is examined for multi-member lanes in this version)" in out


# --- a file that cannot be read is a refusal naming it, not a traceback (the head's follow-on, 1.0526) ----


@pytest.mark.parametrize("which", ["grip.toml", ".grip/workspace_spec.toml"])
def test_a_malformed_pin_file_refuses_naming_the_file(tmp_path: Path, which: str) -> None:
    lane, root, _ = _store_path_lane(tmp_path, pin="pin")
    bad = root / which
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('[[members]\nname = "api"\n')
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "downstream_unreadable"
    assert str(bad) in exc.value.detail and "--no-downstream" in exc.value.detail
    assert not (lane / rr._VENV_DIRNAME).exists() and not (lane / "api").exists()


def test_an_unreadable_pin_file_is_the_same_refusal_and_the_flag_still_skips(tmp_path: Path) -> None:
    lane, root, _ = _store_path_lane(tmp_path, pin="pin")
    (root / "grip.toml").write_bytes(b"\xff\xfe not utf-8 \x00")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"])
    assert exc.value.code == "downstream_unreadable" and "UnicodeDecodeError" in exc.value.detail
    receipt = rr.run_review_lane(lane, pytest_args=["-q"], downstream=False)
    assert receipt["downstream"]["status"] == "skipped"

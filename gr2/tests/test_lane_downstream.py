"""Unchanged workspace members brought into a review lane at their PINS, and never at a remote's tip.

Written by the author of the change (not by an independent reader). Every remote here is built so that its tip
DIVERGES from its pin, because a fixture whose tip equals its pin passes while the code does the wrong thing.
Each row names what must make it go red:
  * an empty pin reaches clone_and_pin (the tip is cloned) -> the empty-pin row
  * an unreachable pin falls back to the tip               -> the unreachable row
  * the post-clone check is removed                        -> the existing-clone row
  * the pin is read from anything but `pin`                -> the pin_of rows
  * downstream selection reads the tip, not the pin        -> the roles-at-the-pin row
  * a refusal leaves the lane looking selected             -> the refusal row
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from gr2.python_cli import clone_exec, lane_downstream as ld
from gr2.python_cli import review_run as rr

from tests.test_review_run_dependency_order import _declare, _witness_lane  # noqa: F401


def _g(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _remote(root: Path, name: str, *, pinned_deps: list[str], tip_deps: list[str]) -> tuple[Path, str, str]:
    """A repo named `name` (distribution `<name>-lib`) with two commits: the PIN declares `pinned_deps`, the
    default-branch TIP declares `tip_deps`. Returns (path, pin, tip)."""
    repo = root / "remotes" / name
    repo.mkdir(parents=True)
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "config", "user.email", "a@e.invalid")
    _g(repo, "config", "user.name", "a")
    shas = []
    for n, deps in enumerate((pinned_deps, tip_deps)):
        (repo / "n").write_text(str(n))  # the two commits always differ, even when the dependencies do not
        listing = ", ".join(json.dumps(d) for d in deps)
        (repo / "pyproject.toml").write_text(
            f'[project]\nname = "{name}-lib"\nversion = "0"\ndependencies = [{listing}]\n'
        )
        _g(repo, "add", "-A")
        _g(repo, "commit", "-qm", "c")
        shas.append(_g(repo, "rev-parse", "HEAD"))
    return repo, shas[0], shas[1]


def _workspace(root: Path, entries: list[dict]) -> Path:
    ws = root / "ws"
    (ws / ".grip").mkdir(parents=True)
    lines = []
    for e in entries:
        lines.append("[[repos]]")
        lines.extend(f"{k} = {json.dumps(v)}" for k, v in e.items())
    (ws / ".grip" / "workspace_spec.toml").write_text("\n".join(lines) + "\n")
    return ws


# --- pin_of: the one accessor ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "main", "abc1234", "A" * 40, "g" * 40, 5, ["a" * 40]])
def test_pin_of_refuses_everything_that_is_not_a_full_lowercase_commit(value) -> None:
    """Goes red if a branch name, a short sha or an uppercase sha counts as a usable pin."""
    assert ld.pin_of({"name": "x", "pin": value}) is None


def test_pin_of_reads_only_the_pin_field_and_returns_a_full_commit() -> None:
    sha = "a1" * 20
    assert ld.pin_of({"name": "x", "pin": sha}) == sha
    assert ld.pin_of({"name": "x", "revision": sha, "commit": sha, "branch": sha}) is None
    assert ld.pin_of({}) is None


# --- materialize_at_pin: one path for every refusal ------------------------------------------------------


def test_a_member_lands_at_its_pin_not_at_the_remote_tip(tmp_path: Path) -> None:
    repo, pin, tip = _remote(tmp_path, "b", pinned_deps=[], tip_deps=["x"])
    assert pin != tip, "the instrument: the tip must diverge from the pin"
    ws = _workspace(tmp_path, [{"name": "b", "path": "b", "url": str(repo), "pin": pin}])
    dest = tmp_path / "lane" / "b"
    assert ld.materialize_at_pin({"name": "b", "url": str(repo), "pin": pin}, dest, workspace_root=ws) == pin
    assert _g(dest, "rev-parse", "HEAD") == pin and (dest / "pyproject.toml").read_text().count("x") == 0


def test_an_empty_pin_refuses_before_any_clone_and_never_takes_the_tip(tmp_path: Path, monkeypatch) -> None:
    """clone_and_pin treats an empty pin as 'stay on the default branch'. Goes red if the empty pin reaches it
    (the sentinel fails the row) or if the tip lands in the lane."""
    repo, pin, tip = _remote(tmp_path, "b", pinned_deps=[], tip_deps=[])

    def never(*a, **k):
        raise AssertionError("clone_and_pin was called for a member with no usable pin")

    monkeypatch.setattr(clone_exec, "clone_and_pin", never)
    dest = tmp_path / "lane" / "b"
    with pytest.raises(ld.PinRefused) as exc:
        ld.materialize_at_pin({"name": "b", "url": str(repo), "pin": ""}, dest, workspace_root=tmp_path)
    assert exc.value.member == "b" and "no usable pin" in exc.value.reason
    assert not dest.exists()


def test_an_unreachable_pin_refuses_the_same_way_and_leaves_no_clone_at_the_tip(tmp_path: Path) -> None:
    repo, pin, tip = _remote(tmp_path, "b", pinned_deps=[], tip_deps=[])
    ghost = "0123456789abcdef" * 2 + "01234567"
    dest = tmp_path / "lane" / "b"
    with pytest.raises(ld.PinRefused) as exc:
        ld.materialize_at_pin({"name": "b", "url": str(repo), "pin": ghost}, dest, workspace_root=tmp_path)
    assert exc.value.member == "b" and ghost[:12] in exc.value.reason
    assert not dest.exists(), "a refusal must not leave a clone at the tip for the next run to accept"
    assert not any(p.name.startswith(".b.staging-") for p in dest.parent.iterdir()), "staging removed"


def test_an_existing_clone_that_is_not_at_the_pin_is_refused_not_trusted(tmp_path: Path) -> None:
    """clone_and_pin returns 'not the first materialization' for a directory that is already a clone, without
    looking at where it is. Goes red if the post-clone check is removed: the lane would hold the tip."""
    repo, pin, tip = _remote(tmp_path, "b", pinned_deps=[], tip_deps=[])
    dest = tmp_path / "lane" / "b"
    dest.parent.mkdir()
    _g(dest.parent, "clone", "-q", str(repo), "b")  # at the tip, the way a reused lane directory might be
    assert _g(dest, "rev-parse", "HEAD") == tip
    with pytest.raises(ld.PinRefused) as exc:
        ld.materialize_at_pin({"name": "b", "url": str(repo), "pin": pin}, dest, workspace_root=tmp_path)
    assert "not at its pin" in exc.value.reason


def test_unchanged_members_are_the_spec_members_the_lane_does_not_bind() -> None:
    spec = {"repos": [{"name": "a"}, {"name": "b"}, {"name": "c"}, "junk", {"path": "nameless"}]}
    assert [r["name"] for r in ld.unchanged_members(spec, ["b"])] == ["a", "c"]


# --- the lane: selection happens at the pins, before any venv --------------------------------------------


def _lane_with_workspace(tmp_path: Path, entries_for) -> tuple[Path, Path]:
    lane = _witness_lane(tmp_path)  # a-web (web-app) and z-core (core-lib), the lane's changed members
    ws = _workspace(tmp_path, entries_for(tmp_path))
    marker_path = lane / rr._MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["workspace_root"] = str(ws)
    marker_path.write_text(json.dumps(marker, indent=2) + "\n")
    return lane, ws


def test_roles_are_selected_from_the_manifest_at_the_pin_not_the_tip(tmp_path: Path) -> None:
    """`api` needs the changed core-lib AT ITS PIN; its tip has dropped that requirement, so reading the tip would
    leave it in no role. `docs` needs nothing in the lane and is removed again. Goes red if selection reads the
    tip, or if an unrelated member stays materialized."""
    api, api_pin, api_tip = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    docs, docs_pin, _ = _remote(tmp_path, "docs", pinned_deps=[], tip_deps=[])
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [
        {"name": "api", "path": "api", "url": str(api), "pin": api_pin},
        {"name": "docs", "path": "docs", "url": str(docs), "pin": docs_pin},
    ])
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    selection = rr._select_downstream(lane, marker, ["a-web", "z-core"])
    assert selection["status"] == "selected"
    assert selection["changed"] == ["a-web", "z-core"]
    assert selection["downstream"] == ["api"] and selection["upstream"] == []
    assert selection["not_selected"] == ["docs"] and selection["pins"] == {"api": api_pin}
    assert _g(lane / "api", "rev-parse", "HEAD") == api_pin and not (lane / "docs").exists()


def test_a_lane_with_no_workspace_or_no_spec_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    lane = _witness_lane(tmp_path)
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    assert rr._select_downstream(lane, marker, ["a-web", "z-core"]) is None  # no workspace_root at all
    marker["workspace_root"] = str(tmp_path / "nowhere")
    assert rr._select_downstream(lane, marker, ["a-web", "z-core"]) is None  # a workspace with no spec


def test_an_unpinned_member_refuses_the_whole_lane_before_a_venv_exists(tmp_path: Path) -> None:
    """The refusal is the code `downstream_unpinned`, names the member, says the way out, leaves no venv, and
    the receipt carries it. Goes red if the lane runs on without the member."""
    api, api_pin, _ = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [
        {"name": "api", "path": "api", "url": str(api)},  # no pin
    ])
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"], downstream=True)
    assert exc.value.code == "downstream_unpinned" and "'api'" in exc.value.detail and "no usable pin" in exc.value.detail
    assert not (lane / rr._VENV_DIRNAME).exists() and not (lane / "api").exists()
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused" and receipt["refusal_code"] == "downstream_unpinned"


def test_an_unreachable_pin_refuses_with_the_same_code_as_a_missing_pin(tmp_path: Path) -> None:
    api, api_pin, _ = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [
        {"name": "api", "path": "api", "url": str(api), "pin": "f" * 40},
    ])
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(lane, pytest_args=["-q"], downstream=True)
    assert exc.value.code == "downstream_unpinned" and "'api'" in exc.value.detail and "ffffffffffff" in exc.value.detail
    assert not (lane / "api").exists()


def test_downstream_is_off_unless_asked_and_the_receipt_is_then_unchanged(tmp_path: Path) -> None:
    """With the parameter off (the default) a lane whose workspace holds an UNPINNED member runs exactly as the
    same lane without a workspace does: same result, no refusal, no `downstream` key, nothing materialized. Goes
    red if the selection runs without being asked."""
    (tmp_path / "control").mkdir()
    control = rr.run_review_lane(_witness_lane(tmp_path / "control"), pytest_args=["-q"])
    api, _, _ = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [{"name": "api", "path": "api", "url": str(api)}])
    receipt = rr.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == control["result"] and receipt["order"] == control["order"]
    assert "downstream" not in receipt and not (lane / "api").exists()

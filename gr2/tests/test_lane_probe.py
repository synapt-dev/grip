"""Downstream reads manifests from a PROBE of each member's pin and clones only the members the plan selects.

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * every unchanged member is cloned in full             -> the unselected-member row (it spies on materialization)
  * the probe copies more than the root-level files      -> the probe-content row
  * the fetched commit is not checked against the pin    -> the HEAD==PIN row
  * the source order is wrong                            -> the source-order rows (url before checkout)
  * a pin found only in a local checkout reads as published -> the unpublished-pin row
  * a refused lane leaves a probe behind                 -> the leftover row
  * the unreadable refusal records not_examined           -> the refused-record row
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from gr2.python_cli import lane_downstream as ld
from gr2.python_cli import review_run as rr

from tests.test_lane_downstream import _g, _lane_with_workspace, _remote
from tests.test_lane_store_path_pins import _store_path_lane, _write_grip_toml


def _entry(name: str, repo: Path, pin: str) -> dict:
    return {"name": name, "path": name, "url": str(repo), "pin": pin}


def test_an_unselected_member_is_never_cloned_in_full(tmp_path: Path, monkeypatch) -> None:
    """`docs` needs nothing in the lane. Goes red if every unchanged member is materialized (the 24 s / 3.6 GB)."""
    api, api_pin, _ = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    docs, docs_pin, _ = _remote(tmp_path, "docs", pinned_deps=[], tip_deps=[])
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [_entry("api", api, api_pin), _entry("docs", docs, docs_pin)])
    cloned: list[str] = []
    real = ld.materialize_at_pin
    monkeypatch.setattr(ld, "materialize_at_pin", lambda spec, *a, **k: (cloned.append(spec["name"]), real(spec, *a, **k))[1])
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    selection, _ = rr._select_downstream(lane, marker, ["a-web", "z-core"])
    assert cloned == ["api"], cloned
    assert selection["not_selected"] == ["docs"] and not (lane / "docs").exists()
    assert selection["downstream"] == ["api"] and (lane / "api" / ".git").exists()
    assert set(selection["sources"]) == {"api", "docs"} and selection["sources"]["docs"]["source"] == "url"


def test_a_probe_holds_the_root_level_files_of_the_pin_and_nothing_else(tmp_path: Path) -> None:
    repo, pin, _ = _remote(tmp_path, "m", pinned_deps=["core-lib>=1"], tip_deps=[])
    (repo / "sub").mkdir()
    (repo / "sub" / "nested.toml").write_text("x = 1\n")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "nested")
    pin = _g(repo, "rev-parse", "HEAD")
    (repo / "big.bin").write_bytes(b"0" * (ld._PROBE_FILE_CAP + 1))
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "big")
    big_pin = _g(repo, "rev-parse", "HEAD")
    dest = tmp_path / "probe"
    probe = ld.probe_at_pin({"name": "m", "url": str(repo), "pin": pin}, dest, workspace_root=tmp_path / "ws")
    names = sorted(p.name for p in dest.iterdir())
    assert "pyproject.toml" in names and "sub" not in names and ".git" not in names and "tip-only" not in names
    assert probe.pin == pin and probe.source == "url" and probe.filtered is True  # a local source is asked to honour the blobless filter
    big = tmp_path / "probe2"
    ld.probe_at_pin({"name": "m", "url": str(repo), "pin": big_pin}, big, workspace_root=tmp_path / "ws")
    assert (big / "pyproject.toml").exists() and not (big / "big.bin").exists()


def test_the_fetched_commit_must_be_the_pin(tmp_path: Path, monkeypatch) -> None:
    """Goes red if the probe trusts a fetch because it succeeded."""
    repo, pin, _ = _remote(tmp_path, "m", pinned_deps=[], tip_deps=[])
    real = ld._git_in

    def fake(cwd, *args, **kw):
        out = real(cwd, *args, **kw)
        if args[:2] == ("rev-parse", "--verify") and "FETCH_HEAD^{commit}" in args:
            return subprocess.CompletedProcess(out.args, 0, ("f" * 40 + "\n").encode(), b"")
        return out

    monkeypatch.setattr(ld, "_git_in", fake)
    with pytest.raises(ld.PinRefused) as exc:
        ld.probe_at_pin({"name": "m", "url": str(repo), "pin": pin}, tmp_path / "p", workspace_root=tmp_path / "ws")
    assert "not the pin" in exc.value.reason


def _spec(repo: Path, pin: str, name: str = "m") -> dict:
    return {"name": name, "url": str(repo), "pin": pin}


def test_the_source_order_is_checkout_then_cache_then_url(tmp_path: Path) -> None:
    repo, pin, _ = _remote(tmp_path, "m", pinned_deps=[], tip_deps=[])
    ws = tmp_path / "ws"
    checkout = ws / "m"
    shutil.copytree(repo, checkout)
    cache = ws / ".grip" / "cache" / "repos" / "m.git"
    cache.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(cache)], check=True)
    spec = _spec(repo, pin)
    assert [s for s, _ in ld.pin_sources(spec, ws)] == ["local checkout", "cache", "url"]
    assert ld.probe_at_pin(spec, tmp_path / "p1", workspace_root=ws).source == "local checkout"
    shutil.rmtree(checkout)
    assert ld.probe_at_pin(spec, tmp_path / "p2", workspace_root=ws).source == "cache"
    shutil.rmtree(cache)
    assert ld.probe_at_pin(spec, tmp_path / "p3", workspace_root=ws).source == "url"


def test_a_pin_in_no_source_refuses_naming_every_source_tried(tmp_path: Path) -> None:
    repo, _, _ = _remote(tmp_path, "m", pinned_deps=[], tip_deps=[])
    with pytest.raises(ld.PinRefused) as exc:
        ld.probe_at_pin(_spec(repo, "e" * 40), tmp_path / "p", workspace_root=tmp_path / "ws")
    assert "was not found in any source" in exc.value.reason and "url:" in exc.value.reason


def test_a_pin_only_a_local_checkout_has_is_found_and_the_receipt_says_so(tmp_path: Path) -> None:
    """The lane's pin is a commit nobody has pushed. Goes red if the source is not recorded, or a green over it
    reads as published: the receipt names the source and says the remote was not checked."""
    api, api_pin, _ = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    stage = tmp_path / "stage-api"
    shutil.copytree(api, stage)
    _g(stage, "reset", "-q", "--hard", api_pin)  # the pin declares the dependency; the remote tip drops it
    (stage / "unpushed.txt").write_text("only here")
    _g(stage, "add", "-A"); _g(stage, "commit", "-qm", "unpushed")
    unpushed = _g(stage, "rev-parse", "HEAD")
    lane, ws = _lane_with_workspace(tmp_path, lambda root: [_entry("api", api, unpushed)])
    shutil.copytree(stage, ws / "api")  # the workspace's own checkout of the member holds the commit; the remote does not
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    selection, _ = rr._select_downstream(lane, marker, ["a-web", "z-core"])
    assert selection["pins"]["api"] == unpushed
    assert selection["sources"]["api"] == {"source": "local checkout", "filtered": True, "published": "not checked"}
    from gr2.python_cli.app import _downstream_line

    line = _downstream_line({**selection, "status": "ran"})
    assert "api taken from the local checkout, not checked against its remote" in line


def test_a_refused_lane_leaves_no_probe_behind(tmp_path: Path) -> None:
    api, api_pin, _ = _remote(tmp_path, "api", pinned_deps=["core-lib>=1"], tip_deps=[])
    lane, _ = _lane_with_workspace(tmp_path, lambda root: [
        _entry("api", api, api_pin), {"name": "ghost", "path": "ghost", "url": str(tmp_path / "nowhere"), "pin": "d" * 40}])
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr._select_downstream(lane, marker, ["a-web", "z-core"])
    assert exc.value.code == "downstream_unpinned" and exc.value.member == "ghost" and "--no-downstream" in exc.value.detail
    assert not (lane / "api").exists() and not (lane / "ghost").exists()


def test_the_unreadable_refusal_records_a_refused_downstream_block(tmp_path: Path) -> None:
    lane, root, _ = _store_path_lane(tmp_path, pin="pin")
    (root / "grip.toml").write_text("[[members\n")
    with pytest.raises(rr.ReviewRunRefused):
        rr.run_review_lane(lane, pytest_args=["-q"])
    receipt = json.loads((lane / rr._RECEIPT_NAME).read_text())
    assert receipt["downstream"]["status"] == "refused" and receipt["downstream"]["code"] == "downstream_unreadable"


def test_a_root_level_symlink_is_not_copied_into_a_probe(tmp_path: Path) -> None:
    """A symlink in the pinned tree is a blob holding a path; writing it as a file would hand a plugin a file the
    member never had. Goes red if only the object kind, not the mode, is checked."""
    repo, pin, _ = _remote(tmp_path, "m", pinned_deps=[], tip_deps=[])
    _g(repo, "reset", "-q", "--hard", pin)
    (repo / "link.toml").symlink_to("pyproject.toml")
    _g(repo, "add", "-A"); _g(repo, "commit", "-qm", "link")
    linked = _g(repo, "rev-parse", "HEAD")
    dest = tmp_path / "probe"
    ld.probe_at_pin({"name": "m", "url": str(repo), "pin": linked}, dest, workspace_root=tmp_path / "ws")
    assert (dest / "pyproject.toml").is_file() and not (dest / "link.toml").exists()

"""Owning allocation and post-deletion recovery using tiny real Git targets."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import open_gr_review as og
from gr2.python_cli import review_allocation as allocation


def _owned(tmp_path: Path, *, owner: str = "workspace"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "review"
    target.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)
    (target / "fruit.txt").write_text("owned\n")
    subprocess.run(["git", "-C", str(target), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "seed"], check=True, capture_output=True)
    allocation.record_created_allocation(workspace, target, owner, "review", [target], disposable=True)
    og.write_open_gr_marker(target, "a" * 40, {}, workspace)
    return workspace, target


def test_marker_without_allocation_never_delegates(tmp_path, monkeypatch):
    workspace, target = _owned(tmp_path)
    allocation.allocation_path(workspace, target).unlink()
    attempts = []
    monkeypatch.setattr(og, "rmtree_or_refuse", lambda path: attempts.append(path))
    with pytest.raises(og.OpenGrReviewError, match="marker alone"):
        og.close_open_gr_lane(target)
    assert attempts == []
    assert (target / "fruit.txt").read_text() == "owned\n"


def test_project_marker_cannot_use_standalone_close(tmp_path, monkeypatch):
    _workspace, target = _owned(tmp_path, owner="project")
    attempts = []
    monkeypatch.setattr(og, "rmtree_or_refuse", lambda path: attempts.append(path))
    with pytest.raises(og.OpenGrReviewError, match="unit differs"):
        og.close_open_gr_lane(target)
    assert attempts == []


def test_post_delete_finalization_failure_retries_without_marker(tmp_path, monkeypatch):
    workspace, target = _owned(tmp_path)
    pending = allocation.allocation_path(workspace, target)
    marker_bytes = (target / og._OPEN_GR_MARKER).read_bytes()
    original_unlink = Path.unlink
    failed = False

    def unlink(path, *args, **kwargs):
        nonlocal failed
        if path == pending and not failed:
            failed = True
            raise OSError("injected allocation finalization failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(og.OpenGrReviewError, match="cleanup incomplete"):
        og.close_open_gr_lane(target)
    assert not target.exists()
    doc = json.loads(pending.read_text())
    assert bytes.fromhex(doc["close_marker_hex"]) == marker_bytes
    assert doc["state"] == "closing"
    attempts = []
    monkeypatch.setattr(og, "rmtree_or_refuse", lambda path: attempts.append(path))
    result = og.close_open_gr_lane(target, workspace_root=workspace)
    assert attempts == []
    assert result["gr_commit"] == "a" * 40
    assert not pending.exists()


def test_destroyed_git_identity_retains_external_authority(tmp_path, monkeypatch):
    workspace, target = _owned(tmp_path)
    pending = allocation.allocation_path(workspace, target)
    original = pending.read_bytes()
    (target / ".git").rename(target / "destroyed-git")
    attempts = []
    monkeypatch.setattr(og, "rmtree_or_refuse", lambda path: attempts.append(path))
    with pytest.raises(og.OpenGrReviewError, match="Git identity unavailable"):
        og.close_open_gr_lane(target)
    assert attempts == []
    assert pending.read_bytes() == original
    assert target.exists()


def test_legacy_adoption_refuses_redirected_managed_ancestor(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign = tmp_path / "foreign"
    target = foreign / "unit" / "review"
    target.mkdir(parents=True)
    (workspace / "reviews").symlink_to(foreign, target_is_directory=True)
    with pytest.raises(allocation.ReviewAllocationError, match="managed workspace boundary"):
        allocation.adopt_legacy_project_allocation(workspace, "unit", "review")
    assert not allocation.allocation_path(workspace, target).exists()
    assert target.exists()


def test_absent_open_allocation_cannot_authorize_cleanup(tmp_path, monkeypatch):
    workspace, target = _owned(tmp_path)
    pending = allocation.allocation_path(workspace, target)
    original = pending.read_bytes()
    # A missing target is recoverable only after the owning close staged CLOSING.
    target.rename(tmp_path / "moved-review")
    attempts = []
    monkeypatch.setattr(og, "rmtree_or_refuse", lambda path: attempts.append(path))
    with pytest.raises(og.OpenGrReviewError, match="open allocation target is absent"):
        og.close_open_gr_lane(target, workspace_root=workspace)
    assert attempts == []
    assert pending.read_bytes() == original


def test_expected_mirror_source_linkage_and_wrong_source_refusal(tmp_path, monkeypatch):
    workspace, member = _owned(tmp_path)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(member), str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "remote", "set-url", "origin", str(source)], check=True, capture_output=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    mirror = cache / "source.git"
    subprocess.run(["git", "clone", "-q", "--mirror", str(source), str(mirror)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(member), "remote", "add", "origin", mirror.as_uri()], check=True, capture_output=True)
    monkeypatch.setenv("SYNAPT_REVIEW_CACHE_ROOT", str(cache))
    expected = "local:" + str(source.resolve())
    assert allocation._legacy_transport_source(member, expected) == expected
    alias = tmp_path / "mirror-alias"
    alias.symlink_to(mirror, target_is_directory=True)
    subprocess.run(["git", "-C", str(member), "remote", "set-url", "origin", alias.as_uri()], check=True, capture_output=True)
    with pytest.raises(ValueError, match="expected mirror"):
        allocation._legacy_transport_source(member, expected)
    subprocess.run(["git", "-C", str(member), "remote", "set-url", "origin", mirror.as_uri()], check=True, capture_output=True)
    assert allocation._legacy_transport_source(member, expected) == expected
    subprocess.run(["git", "-C", str(mirror), "remote", "set-url", "origin", str(tmp_path / "wrong-source")], check=True, capture_output=True)
    with pytest.raises(ValueError, match="mirror source identity differs"):
        allocation._legacy_transport_source(member, expected)
    subprocess.run(["git", "-C", str(mirror), "remote", "set-url", "origin", str(source)], check=True, capture_output=True)
    assert allocation._legacy_transport_source(member, expected) == expected

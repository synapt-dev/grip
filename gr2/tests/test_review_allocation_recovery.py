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
    result = og.close_open_gr_lane(target, workspace_root=workspace)
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

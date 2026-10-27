from __future__ import annotations

from pathlib import Path

import pytest
from gr2.python_cli import events, syncops
from gr2.python_cli.events import EventEmitError
from gr2.python_cli.syncops import SyncIssue, SyncPlan


def _empty_plan(root: Path) -> SyncPlan:
    return SyncPlan(
        workspace_root=str(root),
        spec_path=str(root / ".grip" / "workspace_spec.toml"),
        status="ready",
        dirty_mode="stash",
        dirty_targets=[],
        issues=[],
        operations=[],
    )


def test_after_outcome_sync_dispatch_preserves_completed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        events,
        "emit",
        lambda **_kwargs: (_ for _ in ()).throw(EventEmitError("sink unavailable")),
    )
    syncops._emit_sync_event(
        tmp_path,
        {
            "type": "sync.completed",
            "workspace": tmp_path.name,
            "actor": "system",
            "owner_unit": "workspace",
            "status": "success",
        },
        after_outcome=True,
    )


def test_strict_sync_started_failure_releases_acquired_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = object()
    released: list[object] = []
    monkeypatch.setattr(syncops, "_acquire_sync_lock", lambda _root: lock)
    monkeypatch.setattr(syncops, "_release_sync_lock", released.append)
    # run_sync asks for the spec before it locks; these rows stub the plan, so the spec seam is stubbed with it
    monkeypatch.setattr(syncops, "_require_spec", lambda root: root / ".grip" / "workspace_spec.toml")
    monkeypatch.setattr(syncops, "build_sync_plan", lambda *_args, **_kwargs: _empty_plan(tmp_path))
    monkeypatch.setattr(
        syncops,
        "_emit_sync_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(EventEmitError("sink unavailable")),
    )

    with pytest.raises(EventEmitError, match="sink unavailable"):
        syncops.run_sync(tmp_path)
    assert released == [lock]


def test_strict_blocked_event_failure_releases_acquired_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = object()
    released: list[object] = []
    calls = 0
    blocked_plan = SyncPlan(
        workspace_root=str(tmp_path),
        spec_path=str(tmp_path / ".grip" / "workspace_spec.toml"),
        status="blocked",
        dirty_mode="stash",
        dirty_targets=[],
        issues=[
            SyncIssue(
                level="error",
                code="lease_blocked_sync",
                scope="lane",
                subject="atlas/feat/test:app",
                message="active lease",
                blocks=True,
            )
        ],
        operations=[],
    )
    monkeypatch.setattr(syncops, "_acquire_sync_lock", lambda _root: lock)
    monkeypatch.setattr(syncops, "_release_sync_lock", released.append)
    # run_sync asks for the spec before it locks; these rows stub the plan, so the spec seam is stubbed with it
    monkeypatch.setattr(syncops, "_require_spec", lambda root: root / ".grip" / "workspace_spec.toml")
    monkeypatch.setattr(syncops, "build_sync_plan", lambda *_args, **_kwargs: blocked_plan)

    def fail_second_event(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise EventEmitError("sink unavailable")

    monkeypatch.setattr(syncops, "_emit_sync_event", fail_second_event)

    with pytest.raises(EventEmitError, match="sink unavailable"):
        syncops.run_sync(tmp_path)
    assert released == [lock]

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_abort_unit_rolls_back_inflight_transaction_and_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.units as units

    workspace_root = tmp_path / "workspace"
    _write_inflight_state(
        workspace_root,
        "feature-auth",
        {
            "unit_name": "feature-auth",
            "repo_order": ["app", "api"],
            "completed_repos": ["app"],
            "failing_repo": "api",
        },
    )

    observed: list[dict[str, object]] = []

    def fake_rollback_inflight_unit(*, workspace_root: Path, state: dict[str, object]):
        observed.append(state)
        return {"status": "rolled_back", "rolled_back_repos": ["app", "api"]}

    monkeypatch.setattr(units, "rollback_inflight_unit", fake_rollback_inflight_unit)

    result = units.abort_unit(workspace_root=workspace_root, unit_name="feature-auth")

    assert result["status"] == "rolled_back"
    assert result["rolled_back_repos"] == ["app", "api"]
    assert observed[0]["unit_name"] == "feature-auth"
    assert not (workspace_root / ".grip" / "unit-transactions" / "feature-auth.json").exists()


def test_abort_unit_rejects_missing_inflight_state(tmp_path: Path) -> None:
    from gr2_overlay.units import abort_unit

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    with pytest.raises(FileNotFoundError, match="feature-auth"):
        abort_unit(workspace_root=workspace_root, unit_name="feature-auth")


def test_abort_unit_preserves_state_file_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.units as units

    workspace_root = tmp_path / "workspace"
    state_path = _write_inflight_state(
        workspace_root,
        "feature-auth",
        {
            "unit_name": "feature-auth",
            "repo_order": ["app"],
            "completed_repos": [],
            "failing_repo": "app",
        },
    )

    def fake_rollback_inflight_unit(*, workspace_root: Path, state: dict[str, object]):
        raise RuntimeError("rollback failed")

    monkeypatch.setattr(units, "rollback_inflight_unit", fake_rollback_inflight_unit)

    with pytest.raises(RuntimeError, match="rollback failed"):
        units.abort_unit(workspace_root=workspace_root, unit_name="feature-auth")

    assert state_path.exists()


def _write_inflight_state(
    workspace_root: Path,
    unit_name: str,
    payload: dict[str, object],
) -> Path:
    path = workspace_root / ".grip" / "unit-transactions" / f"{unit_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path

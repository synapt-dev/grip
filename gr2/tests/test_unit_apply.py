from __future__ import annotations

from pathlib import Path

import pytest


def test_apply_unit_delegates_manifest_targets_to_atomic_overlay_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.units as units
    from gr2_overlay.cross_repo import CrossRepoActivationResult

    workspace_root = tmp_path / "workspace"
    _write_named_manifest(
        workspace_root,
        "base-theme",
        """
version = 1
scope = "workspace"
target_base_ref = "refs/heads/main"
depends_on = []
on_failure = "rollback"

[[source_overlays]]
repo_name = "app"
overlay_ref = "refs/overlays/team/base-theme"
overlay_source_kind = "path"
overlay_source_value = "team/base-theme"
""",
    )
    _write_manifest(
        workspace_root,
        """
version = 1
scope = "workspace"
target_base_ref = "refs/heads/main"
depends_on = ["base-theme"]
on_failure = "rollback"

[[source_overlays]]
repo_name = "app"
overlay_ref = "refs/overlays/team/feature-auth"
overlay_source_kind = "path"
overlay_source_value = "team/feature-auth"
overlay_signer = "unsigned"

[[source_overlays]]
repo_name = "api"
overlay_ref = "refs/overlays/team/feature-auth"
overlay_source_kind = "path"
overlay_source_value = "team/feature-auth"
""",
    )

    calls: list[list[object]] = []

    def fake_activate_overlays_atomically(*, targets):
        calls.append(targets)
        return CrossRepoActivationResult(status="ok", completed_repos=["app", "api"])

    monkeypatch.setattr(units, "activate_overlays_atomically", fake_activate_overlays_atomically)

    result = units.apply_unit(
        workspace_root=workspace_root,
        unit_name="feature-auth",
    )

    assert result["status"] == "ok"
    assert result["applied_units"] == ["base-theme", "feature-auth"]
    assert len(calls) == 2
    delegated = calls[1]
    assert [target.repo_name for target in delegated] == ["app", "api"]
    assert delegated[0].overlay_ref.ref_path == "refs/overlays/team/feature-auth"
    assert delegated[0].overlay_source_kind == "path"
    assert delegated[0].overlay_source_value == "team/feature-auth"
    assert delegated[0].overlay_signer == "unsigned"
    assert delegated[1].overlay_signer is None


def test_apply_unit_surfaces_atomic_failure_details_without_rewriting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.units as units
    from gr2_overlay.cross_repo import CrossRepoActivationError

    workspace_root = tmp_path / "workspace"
    _write_manifest(
        workspace_root,
        """
version = 1
scope = "workspace"
target_base_ref = "refs/heads/main"
depends_on = []
on_failure = "rollback"

[[source_overlays]]
repo_name = "app"
overlay_ref = "refs/overlays/team/feature-auth"
overlay_source_kind = "path"
overlay_source_value = "team/feature-auth"
""",
    )

    def fake_activate_overlays_atomically(*, targets):
        raise CrossRepoActivationError(
            "failed",
            error_code="base_advanced",
            failing_repo="app",
            rolled_back_repos=[],
        )

    monkeypatch.setattr(units, "activate_overlays_atomically", fake_activate_overlays_atomically)

    with pytest.raises(CrossRepoActivationError) as exc:
        units.apply_unit(workspace_root=workspace_root, unit_name="feature-auth")

    assert exc.value.error_code == "base_advanced"
    assert exc.value.failing_repo == "app"
    assert exc.value.rolled_back_repos == []


def test_apply_unit_rejects_missing_manifest(tmp_path: Path) -> None:
    from gr2_overlay.units import apply_unit

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    with pytest.raises(FileNotFoundError, match="feature-auth"):
        apply_unit(workspace_root=workspace_root, unit_name="feature-auth")


def _write_manifest(workspace_root: Path, body: str) -> None:
    _write_named_manifest(workspace_root, "feature-auth", body)


def _write_named_manifest(workspace_root: Path, name: str, body: str) -> None:
    path = workspace_root / ".grip" / "units" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip())

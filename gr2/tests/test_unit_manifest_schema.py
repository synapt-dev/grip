from __future__ import annotations

from pathlib import Path

import pytest

from gr2_overlay.types import OverlayRef


def test_unit_manifest_path_lives_under_grip_units_directory(tmp_path: Path) -> None:
    from gr2_overlay.units import unit_manifest_path

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    assert unit_manifest_path(workspace_root, "feature-auth") == (
        workspace_root / ".grip" / "units" / "feature-auth.toml"
    )


def test_load_parses_unit_manifest_with_all_required_fields(tmp_path: Path) -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, load_unit_manifest

    workspace_root = tmp_path / "workspace"
    manifest_path = workspace_root / ".grip" / "units" / "feature-auth.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
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
"""
    )

    manifest = load_unit_manifest(workspace_root, "feature-auth")

    assert manifest == UnitManifest(
        version=1,
        scope="workspace",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer="unsigned",
            ),
            UnitOverlaySource(
                repo_name="api",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            ),
        ],
        target_base_ref="refs/heads/main",
        depends_on=["base-theme"],
        on_failure="rollback",
    )


def test_depends_on_defaults_to_empty_list_when_omitted(tmp_path: Path) -> None:
    from gr2_overlay.units import load_unit_manifest

    workspace_root = tmp_path / "workspace"
    manifest_path = workspace_root / ".grip" / "units" / "feature-auth.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        """
version = 1
scope = "repo"
target_base_ref = "refs/heads/feat-auth"
on_failure = "rollback"

[[source_overlays]]
repo_name = "app"
overlay_ref = "refs/overlays/team/feature-auth"
overlay_source_kind = "path"
overlay_source_value = "team/feature-auth"
"""
    )

    manifest = load_unit_manifest(workspace_root, "feature-auth")
    assert manifest.depends_on == []


def test_validate_rejects_non_v1_manifest() -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, validate_unit_manifest

    manifest = UnitManifest(
        version=2,
        scope="workspace",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            )
        ],
        target_base_ref="refs/heads/main",
        depends_on=[],
        on_failure="rollback",
    )

    with pytest.raises(ValueError, match="version"):
        validate_unit_manifest(manifest)


def test_validate_rejects_invalid_scope() -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, validate_unit_manifest

    manifest = UnitManifest(
        version=1,
        scope="lane",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            )
        ],
        target_base_ref="refs/heads/main",
        depends_on=[],
        on_failure="rollback",
    )

    with pytest.raises(ValueError, match="scope"):
        validate_unit_manifest(manifest)


def test_validate_rejects_empty_source_overlays() -> None:
    from gr2_overlay.units import UnitManifest, validate_unit_manifest

    manifest = UnitManifest(
        version=1,
        scope="workspace",
        source_overlays=[],
        target_base_ref="refs/heads/main",
        depends_on=[],
        on_failure="rollback",
    )

    with pytest.raises(ValueError, match="source_overlays"):
        validate_unit_manifest(manifest)


def test_validate_rejects_duplicate_repo_names_in_source_overlays() -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, validate_unit_manifest

    manifest = UnitManifest(
        version=1,
        scope="workspace",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="base"),
                overlay_source_kind="path",
                overlay_source_value="team/base",
                overlay_signer=None,
            ),
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            ),
        ],
        target_base_ref="refs/heads/main",
        depends_on=[],
        on_failure="rollback",
    )

    with pytest.raises(ValueError, match="Duplicate source overlay repo_name"):
        validate_unit_manifest(manifest)


def test_validate_rejects_empty_target_base_ref() -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, validate_unit_manifest

    manifest = UnitManifest(
        version=1,
        scope="workspace",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            )
        ],
        target_base_ref="",
        depends_on=[],
        on_failure="rollback",
    )

    with pytest.raises(ValueError, match="target_base_ref"):
        validate_unit_manifest(manifest)


def test_validate_rejects_invalid_on_failure_policy() -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, validate_unit_manifest

    manifest = UnitManifest(
        version=1,
        scope="workspace",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            )
        ],
        target_base_ref="refs/heads/main",
        depends_on=[],
        on_failure="skip",
    )

    with pytest.raises(ValueError, match="on_failure"):
        validate_unit_manifest(manifest)


def test_validate_accepts_repo_scope_and_named_dependencies() -> None:
    from gr2_overlay.units import UnitManifest, UnitOverlaySource, validate_unit_manifest

    manifest = UnitManifest(
        version=1,
        scope="repo",
        source_overlays=[
            UnitOverlaySource(
                repo_name="app",
                overlay_ref=OverlayRef(author="team", name="feature-auth"),
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            )
        ],
        target_base_ref="refs/heads/feat-auth",
        depends_on=["base-theme", "auth-primitives"],
        on_failure="rollback",
    )

    validate_unit_manifest(manifest)

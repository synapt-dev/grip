from __future__ import annotations

from pathlib import Path

import pytest

from gr2_overlay.trust import (
    OverlayTrustError,
    TrustClass,
    TrustSource,
    authorize_overlay_driver,
    can_diff_overlay,
    can_inspect_overlay,
    load_workspace_allowlist,
    trust_config_path,
)
from gr2_overlay.types import OverlayRef


def test_trust_config_lives_in_workspace_grip_directory(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    assert trust_config_path(workspace_root) == workspace_root / ".grip" / "trust.toml"


def test_workspace_allowlist_loads_local_team_and_signed_sources(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    trust_path = workspace_root / ".grip" / "trust.toml"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text(
        """
[[source]]
kind = "path"
pattern = "vendor/*"
trust_class = "local"

[[source]]
kind = "path"
pattern = "team/*"
trust_class = "team"

[[source]]
kind = "signed"
signer = "minisign:atlas-team"
trust_class = "team"
"""
    )

    allowlist = load_workspace_allowlist(workspace_root)
    assert allowlist == [
        TrustSource(kind="path", pattern="vendor/*", signer=None, trust_class=TrustClass.LOCAL),
        TrustSource(kind="path", pattern="team/*", signer=None, trust_class=TrustClass.TEAM),
        TrustSource(
            kind="signed",
            pattern=None,
            signer="minisign:atlas-team",
            trust_class=TrustClass.TEAM,
        ),
    ]


def test_allowlisted_source_allows_curated_driver_execution(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    trust_path = workspace_root / ".grip" / "trust.toml"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text(
        """
[[source]]
kind = "path"
pattern = "team/*"
trust_class = "team"
"""
    )

    allowlist = load_workspace_allowlist(workspace_root)
    source_overlay = OverlayRef(author="team", name="shared-config")

    trust_class = authorize_overlay_driver(
        driver_name="overlay-union",
        overlay_ref=source_overlay,
        overlay_source_kind="path",
        overlay_source_value="team/shared-config",
        overlay_signer=None,
        allowlist=allowlist,
    )

    assert trust_class == TrustClass.TEAM


def test_unallowlisted_source_blocks_driver_with_overlay_untrusted(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    trust_path = workspace_root / ".grip" / "trust.toml"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text(
        """
[[source]]
kind = "path"
pattern = "team/*"
trust_class = "team"
"""
    )

    allowlist = load_workspace_allowlist(workspace_root)
    source_overlay = OverlayRef(author="third-party", name="theme-pack")

    with pytest.raises(OverlayTrustError) as exc:
        authorize_overlay_driver(
            driver_name="overlay-union",
            overlay_ref=source_overlay,
            overlay_source_kind="path",
            overlay_source_value="vendor/theme-pack",
            overlay_signer=None,
            allowlist=allowlist,
        )

    assert exc.value.error_code == "overlay_untrusted"
    assert "allowlist" in str(exc.value)


def test_unallowlisted_source_still_allows_diff_and_inspect(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    trust_path = workspace_root / ".grip" / "trust.toml"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text("")

    allowlist = load_workspace_allowlist(workspace_root)
    source_overlay = OverlayRef(author="third-party", name="theme-pack")

    assert can_inspect_overlay(
        overlay_ref=source_overlay,
        overlay_source_kind="path",
        overlay_source_value="vendor/theme-pack",
        overlay_signer=None,
        allowlist=allowlist,
    )
    assert can_diff_overlay(
        overlay_ref=source_overlay,
        overlay_source_kind="path",
        overlay_source_value="vendor/theme-pack",
        overlay_signer=None,
        allowlist=allowlist,
    )


def test_unknown_driver_is_refused_even_for_trusted_source(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    trust_path = workspace_root / ".grip" / "trust.toml"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text(
        """
[[source]]
kind = "signed"
signer = "minisign:atlas-team"
trust_class = "team"
"""
    )

    allowlist = load_workspace_allowlist(workspace_root)
    source_overlay = OverlayRef(author="team", name="shared-config")

    with pytest.raises(ValueError):
        authorize_overlay_driver(
            driver_name="overlay-rce",
            overlay_ref=source_overlay,
            overlay_source_kind="signed",
            overlay_source_value=None,
            overlay_signer="minisign:atlas-team",
            allowlist=allowlist,
        )


def test_trust_model_treats_gitattributes_as_metadata_not_authority(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    trust_path = workspace_root / ".grip" / "trust.toml"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_text("")

    allowlist = load_workspace_allowlist(workspace_root)
    source_overlay = OverlayRef(author="mallory", name="theme-pack")

    with pytest.raises(OverlayTrustError) as exc:
        authorize_overlay_driver(
            driver_name="overlay-prepend",
            overlay_ref=source_overlay,
            overlay_source_kind="path",
            overlay_source_value="vendor/mallory-pack",
            overlay_signer=None,
            allowlist=allowlist,
            declared_driver="overlay-prepend",
        )

    assert exc.value.error_code == "overlay_untrusted"
    assert "metadata" in str(exc.value)

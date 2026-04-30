from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_propose_unit_manifest_writes_schema_from_current_overlay_state(tmp_path: Path) -> None:
    from gr2_overlay.units import RepoUnitSource, propose_unit_manifest

    workspace_root = tmp_path / "workspace"
    app_root = workspace_root / "repos" / "app"
    api_root = workspace_root / "repos" / "api"
    _write_active_stack(app_root, ["refs/overlays/team/feature-auth"])
    _write_active_stack(api_root, ["refs/overlays/team/feature-auth"])

    manifest = propose_unit_manifest(
        workspace_root=workspace_root,
        unit_name="feature-auth",
        scope="workspace",
        target_base_ref="refs/heads/main",
        source_repos=[
            RepoUnitSource(
                repo_name="app",
                repo_root=app_root,
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer="unsigned",
            ),
            RepoUnitSource(
                repo_name="api",
                repo_root=api_root,
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            ),
        ],
        depends_on=["base-theme"],
        on_failure="rollback",
    )

    manifest_path = workspace_root / ".grip" / "units" / "feature-auth.toml"
    assert manifest_path.exists()
    text = manifest_path.read_text()
    assert 'version = 1' in text
    assert 'scope = "workspace"' in text
    assert 'target_base_ref = "refs/heads/main"' in text
    assert 'on_failure = "rollback"' in text
    assert 'depends_on = ["base-theme"]' in text
    assert 'repo_name = "app"' in text
    assert 'repo_name = "api"' in text
    assert manifest.source_overlays[0].overlay_ref.ref_path == "refs/overlays/team/feature-auth"
    assert manifest.source_overlays[1].overlay_ref.ref_path == "refs/overlays/team/feature-auth"


def test_propose_unit_manifest_rejects_repo_without_active_overlay(tmp_path: Path) -> None:
    from gr2_overlay.units import RepoUnitSource, propose_unit_manifest

    workspace_root = tmp_path / "workspace"
    app_root = workspace_root / "repos" / "app"
    app_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="no active overlay"):
        propose_unit_manifest(
            workspace_root=workspace_root,
            unit_name="feature-auth",
            scope="workspace",
            target_base_ref="refs/heads/main",
            source_repos=[
                RepoUnitSource(
                    repo_name="app",
                    repo_root=app_root,
                    overlay_source_kind="path",
                    overlay_source_value="team/feature-auth",
                    overlay_signer=None,
                )
            ],
            depends_on=[],
            on_failure="rollback",
        )


def test_propose_unit_manifest_rejects_multiple_active_overlays_for_single_repo(
    tmp_path: Path,
) -> None:
    from gr2_overlay.units import RepoUnitSource, propose_unit_manifest

    workspace_root = tmp_path / "workspace"
    app_root = workspace_root / "repos" / "app"
    _write_active_stack(
        app_root,
        [
            "refs/overlays/team/base-theme",
            "refs/overlays/team/feature-auth",
        ],
    )

    with pytest.raises(ValueError, match="exactly one active overlay"):
        propose_unit_manifest(
            workspace_root=workspace_root,
            unit_name="feature-auth",
            scope="repo",
            target_base_ref="refs/heads/feat-auth",
            source_repos=[
                RepoUnitSource(
                    repo_name="app",
                    repo_root=app_root,
                    overlay_source_kind="path",
                    overlay_source_value="team/feature-auth",
                    overlay_signer=None,
                )
            ],
            depends_on=[],
            on_failure="rollback",
        )


def test_propose_unit_manifest_rejects_conflicting_existing_manifest(tmp_path: Path) -> None:
    from gr2_overlay.units import RepoUnitSource, propose_unit_manifest

    workspace_root = tmp_path / "workspace"
    app_root = workspace_root / "repos" / "app"
    _write_active_stack(app_root, ["refs/overlays/team/feature-auth"])

    source_repos = [
        RepoUnitSource(
            repo_name="app",
            repo_root=app_root,
            overlay_source_kind="path",
            overlay_source_value="team/feature-auth",
            overlay_signer=None,
        )
    ]

    propose_unit_manifest(
        workspace_root=workspace_root,
        unit_name="feature-auth",
        scope="workspace",
        target_base_ref="refs/heads/main",
        source_repos=source_repos,
        depends_on=[],
        on_failure="rollback",
    )

    with pytest.raises(ValueError, match="already exists with different content"):
        propose_unit_manifest(
            workspace_root=workspace_root,
            unit_name="feature-auth",
            scope="repo",
            target_base_ref="refs/heads/dev",
            source_repos=source_repos,
            depends_on=["base-theme"],
            on_failure="rollback",
        )


def test_propose_unit_manifest_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    from gr2_overlay.units import RepoUnitSource, propose_unit_manifest

    workspace_root = tmp_path / "workspace"
    app_root = workspace_root / "repos" / "app"
    _write_active_stack(app_root, ["refs/overlays/team/feature-auth"])

    kwargs = dict(
        workspace_root=workspace_root,
        unit_name="feature-auth",
        scope="workspace",
        target_base_ref="refs/heads/main",
        source_repos=[
            RepoUnitSource(
                repo_name="app",
                repo_root=app_root,
                overlay_source_kind="path",
                overlay_source_value="team/feature-auth",
                overlay_signer=None,
            )
        ],
        depends_on=[],
        on_failure="rollback",
    )

    first = propose_unit_manifest(**kwargs)
    second = propose_unit_manifest(**kwargs)

    assert first.source_overlays[0].overlay_ref.ref_path == second.source_overlays[0].overlay_ref.ref_path


def _write_active_stack(repo_root: Path, refs: list[str]) -> None:
    grip_dir = repo_root / ".grip"
    grip_dir.mkdir(parents=True, exist_ok=True)
    (grip_dir / "overlay-stack.json").write_text(json.dumps(refs))

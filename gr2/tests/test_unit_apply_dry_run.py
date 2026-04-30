from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_preview_unit_apply_reports_repo_order_and_manifest_surface_without_mutation(
    tmp_path: Path,
) -> None:
    from gr2_overlay.units import preview_unit_apply

    workspace_root = tmp_path / "workspace"
    _write_manifest(
        workspace_root,
        "feature-auth",
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

[[source_overlays]]
repo_name = "api"
overlay_ref = "refs/overlays/team/feature-auth"
overlay_source_kind = "path"
overlay_source_value = "team/feature-auth"
""",
    )
    _write_active_stack(workspace_root / "repos" / "app", ["refs/overlays/team/base-theme"])
    _write_active_stack(workspace_root / "repos" / "api", ["refs/overlays/team/base-theme"])

    before = _snapshot(workspace_root)

    preview = preview_unit_apply(
        workspace_root=workspace_root,
        unit_name="feature-auth",
    )

    assert preview.status == "ok"
    assert preview.unit_name == "feature-auth"
    assert preview.scope == "workspace"
    assert preview.target_base_ref == "refs/heads/main"
    assert preview.on_failure == "rollback"
    assert preview.depends_on == ["base-theme"]
    assert preview.repo_order == ["app", "api"]
    assert preview.overlay_refs == [
        "refs/overlays/team/feature-auth",
        "refs/overlays/team/feature-auth",
    ]
    assert _snapshot(workspace_root) == before


def test_preview_unit_apply_rejects_missing_manifest(tmp_path: Path) -> None:
    from gr2_overlay.units import preview_unit_apply

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    with pytest.raises(FileNotFoundError, match="feature-auth"):
        preview_unit_apply(workspace_root=workspace_root, unit_name="feature-auth")


def test_preview_unit_apply_rejects_manifest_with_unknown_failure_policy(tmp_path: Path) -> None:
    from gr2_overlay.units import preview_unit_apply

    workspace_root = tmp_path / "workspace"
    _write_manifest(
        workspace_root,
        "feature-auth",
        """
version = 1
scope = "workspace"
target_base_ref = "refs/heads/main"
depends_on = []
on_failure = "skip"

[[source_overlays]]
repo_name = "app"
overlay_ref = "refs/overlays/team/feature-auth"
overlay_source_kind = "path"
overlay_source_value = "team/feature-auth"
""",
    )

    with pytest.raises(ValueError, match="on_failure"):
        preview_unit_apply(workspace_root=workspace_root, unit_name="feature-auth")


def _write_manifest(workspace_root: Path, unit_name: str, body: str) -> None:
    path = workspace_root / ".grip" / "units" / f"{unit_name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip())


def _write_active_stack(repo_root: Path, refs: list[str]) -> None:
    grip_dir = repo_root / ".grip"
    grip_dir.mkdir(parents=True, exist_ok=True)
    (grip_dir / "overlay-stack.json").write_text(json.dumps(refs))


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_text()
    return result

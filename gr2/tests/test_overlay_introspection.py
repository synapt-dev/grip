from __future__ import annotations

from pathlib import Path

from gr2_overlay.introspection import (
    overlay_impact,
    overlay_stack,
    overlay_status,
    overlay_trace,
    overlay_why,
)
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.refs import push_overlay_ref
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_overlay_stack_reports_active_stack_with_metadata_in_human_and_json(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = tmp_path / "workspace"
    source_root = tmp_path / "source"
    workspace_root.mkdir()
    source_root.mkdir()

    active_ref = OverlayRef(author="atlas", name="theme-dark")
    available_ref = OverlayRef(author="team", name="shared-base")
    _capture_compose_overlay(overlay_store, source_root, active_ref)
    _capture_compose_overlay(overlay_store, source_root, available_ref)
    _write_file(
        workspace_root / ".grip" / "overlay-stack.toml",
        'active = ["refs/overlays/atlas/theme-dark"]\navailable = ["refs/overlays/team/shared-base"]\n',
    )

    human = overlay_stack(workspace_root=workspace_root, overlay_store=overlay_store, json_output=False)
    machine = overlay_stack(workspace_root=workspace_root, overlay_store=overlay_store, json_output=True)

    assert "atlas/theme-dark" in human
    assert "active" in human.lower()
    assert machine["active"][0]["ref"] == "refs/overlays/atlas/theme-dark"
    assert machine["available"][0]["ref"] == "refs/overlays/team/shared-base"
    assert machine["active"][0]["author"] == "atlas"


def test_overlay_trace_attributes_lines_to_overlay_regions(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = tmp_path / "workspace"
    source_root = tmp_path / "source"
    workspace_root.mkdir()
    source_root.mkdir()

    overlay_ref = OverlayRef(author="atlas", name="theme-dark")
    _write_file(source_root / "settings.toml", 'theme = "owl"\naccent = "teal"\n')
    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))
    _write_file(workspace_root / "settings.toml", 'theme = "owl"\naccent = "teal"\n')
    _write_file(
        workspace_root / ".grip" / "overlay-attribution.toml",
        '[files."settings.toml"]\nlines = [{start = 1, end = 2, ref = "refs/overlays/atlas/theme-dark"}]\n',
    )

    human = overlay_trace(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        file_path="settings.toml",
        json_output=False,
    )
    machine = overlay_trace(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        file_path="settings.toml",
        json_output=True,
    )

    assert "settings.toml" in human
    assert "refs/overlays/atlas/theme-dark" in human
    assert machine["file"] == "settings.toml"
    assert machine["regions"][0] == {
        "start": 1,
        "end": 2,
        "ref": "refs/overlays/atlas/theme-dark",
    }


def test_overlay_why_reports_winning_merge_rule_and_reason(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = tmp_path / "workspace"
    source_root = tmp_path / "source"
    workspace_root.mkdir()
    source_root.mkdir()

    overlay_ref = OverlayRef(author="atlas", name="theme-dark")
    _capture_compose_overlay(overlay_store, source_root, overlay_ref)
    _write_file(
        workspace_root / ".grip" / "overlay-why.toml",
        '[files."settings.toml"]\nrule = "overlay-deep"\nreason = "matched *.toml via curated driver registry"\nref = "refs/overlays/atlas/theme-dark"\n',
    )

    human = overlay_why(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        file_path="settings.toml",
        json_output=False,
    )
    machine = overlay_why(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        file_path="settings.toml",
        json_output=True,
    )

    assert "overlay-deep" in human
    assert "*.toml" in human
    assert machine["rule"] == "overlay-deep"
    assert "curated driver registry" in machine["reason"]
    assert machine["ref"] == "refs/overlays/atlas/theme-dark"


def test_overlay_impact_lists_files_touched_by_overlay(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = tmp_path / "source"
    source_root.mkdir()

    overlay_ref = OverlayRef(author="atlas", name="theme-dark")
    _write_file(source_root / "COMPOSE.md", "compose\n")
    _write_file(source_root / "settings.toml", 'theme = "owl"\n')
    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))

    human = overlay_impact(overlay_store=overlay_store, overlay_ref=overlay_ref, json_output=False)
    machine = overlay_impact(overlay_store=overlay_store, overlay_ref=overlay_ref, json_output=True)

    assert "COMPOSE.md" in human
    assert "settings.toml" in human
    assert sorted(machine["files"]) == ["COMPOSE.md", "settings.toml"]


def test_overlay_status_reports_active_available_and_applied_sets(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "local-store.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote-store.git")
    workspace_root = tmp_path / "workspace"
    source_root = tmp_path / "source"
    workspace_root.mkdir()
    source_root.mkdir()

    active_ref = OverlayRef(author="atlas", name="theme-dark")
    available_ref = OverlayRef(author="team", name="shared-base")
    applied_ref = OverlayRef(author="atlas", name="already-applied")

    _capture_compose_overlay(overlay_store, source_root, active_ref)
    _capture_compose_overlay(overlay_store, source_root, available_ref)
    _capture_compose_overlay(overlay_store, source_root, applied_ref)
    push_overlay_ref(overlay_store, remote_store, available_ref)

    _write_file(
        workspace_root / ".grip" / "overlay-status.toml",
        'active = ["refs/overlays/atlas/theme-dark"]\navailable = ["refs/overlays/team/shared-base"]\napplied = ["refs/overlays/atlas/already-applied"]\n',
    )

    human = overlay_status(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        json_output=False,
    )
    machine = overlay_status(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        json_output=True,
    )

    assert "active" in human.lower()
    assert "available" in human.lower()
    assert "applied" in human.lower()
    assert machine["active"] == ["refs/overlays/atlas/theme-dark"]
    assert machine["available"] == ["refs/overlays/team/shared-base"]
    assert machine["applied"] == ["refs/overlays/atlas/already-applied"]


def _capture_compose_overlay(overlay_store: Path, source_root: Path, overlay_ref: OverlayRef) -> None:
    _write_file(source_root / "COMPOSE.md", "compose\n")
    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))


def _overlay_meta(overlay_ref: OverlayRef) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author=overlay_ref.author,
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=[],
    )


def _init_bare_git_repo(path: Path) -> Path:
    import subprocess

    subprocess.run(
        ["git", "init", "--bare", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)

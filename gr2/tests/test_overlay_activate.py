from __future__ import annotations

from pathlib import Path

import pytest

from gr2_overlay.activate import (
    OverlayActivationError,
    activate_overlay,
    deactivate_overlay,
    read_active_overlay_stack,
)
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.refs import fetch_overlay_ref, push_overlay_ref
from gr2_overlay.trust import write_workspace_allowlist
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_activate_materializes_overlay_eagerly_into_working_tree(tmp_path: Path) -> None:
    overlay_store, workspace_root, source_root = _workspace_triplet(tmp_path)

    overlay_ref = OverlayRef(author="atlas", name="theme-dark")
    metadata = _overlay_meta(overlay_ref)
    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "settings.toml", 'theme = "owl"\n')

    capture_overlay_object(overlay_store, source_root, metadata)
    write_workspace_allowlist(workspace_root, [{"kind": "path", "pattern": "atlas/*", "trust_class": "local"}])

    result = activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/theme-dark",
        overlay_signer=None,
    )

    assert result.status == "ok"
    assert result.completed == ["overlay.activated"]
    assert (workspace_root / "COMPOSE.md").read_text() == "overlay compose\n"
    assert (workspace_root / "settings.toml").read_text() == 'theme = "owl"\n'


def test_activate_twice_is_idempotent_and_reversible_via_deactivate(tmp_path: Path) -> None:
    overlay_store, workspace_root, source_root = _workspace_triplet(tmp_path)

    overlay_ref = OverlayRef(author="atlas", name="compose-overlay")
    _write_file(workspace_root / "base.toml", 'base = "keep"\n')
    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "settings.toml", 'theme = "owl"\n')

    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))
    write_workspace_allowlist(workspace_root, [{"kind": "path", "pattern": "atlas/*", "trust_class": "local"}])

    first = activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/compose-overlay",
        overlay_signer=None,
    )
    first_snapshot = _snapshot(workspace_root)

    second = activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/compose-overlay",
        overlay_signer=None,
    )
    second_snapshot = _snapshot(workspace_root)

    deactivated = deactivate_overlay(
        workspace_root=workspace_root,
        overlay_ref=overlay_ref,
    )
    after_deactivate = _snapshot(workspace_root)

    third = activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/compose-overlay",
        overlay_signer=None,
    )
    third_snapshot = _snapshot(workspace_root)

    assert first.status == "ok"
    assert second.status == "ok"
    assert third.status == "ok"
    assert second_snapshot == first_snapshot
    assert after_deactivate == {"base.toml": 'base = "keep"\n'}
    assert third_snapshot == first_snapshot
    assert deactivated.completed == ["overlay.deactivated"]


def test_activate_and_deactivate_mutate_workspace_overlay_stack(tmp_path: Path) -> None:
    overlay_store, workspace_root, source_root = _workspace_triplet(tmp_path)
    overlay_ref = OverlayRef(author="atlas", name="stacked")
    _write_file(source_root / "COMPOSE.md", "overlay compose\n")

    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))
    write_workspace_allowlist(workspace_root, [{"kind": "path", "pattern": "atlas/*", "trust_class": "local"}])

    assert read_active_overlay_stack(workspace_root) == []

    activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/stacked",
        overlay_signer=None,
    )
    assert read_active_overlay_stack(workspace_root) == ["refs/overlays/atlas/stacked"]

    deactivate_overlay(workspace_root=workspace_root, overlay_ref=overlay_ref)
    assert read_active_overlay_stack(workspace_root) == []


def test_activate_blocks_untrusted_overlay_before_driver_execution(tmp_path: Path) -> None:
    overlay_store, workspace_root, source_root = _workspace_triplet(tmp_path)
    overlay_ref = OverlayRef(author="third-party", name="theme-pack")
    _write_file(source_root / "COMPOSE.md", "overlay compose\n")

    capture_overlay_object(
        overlay_store,
        source_root,
        _overlay_meta(overlay_ref, trust=TrustLevel.UNTRUSTED),
    )
    write_workspace_allowlist(workspace_root, [])

    with pytest.raises(OverlayActivationError) as exc:
        activate_overlay(
            workspace_root=workspace_root,
            overlay_store=overlay_store,
            overlay_ref=overlay_ref,
            overlay_source_kind="path",
            overlay_source_value="vendor/theme-pack",
            overlay_signer=None,
        )

    assert exc.value.error_code == "overlay_untrusted"
    assert not (workspace_root / "COMPOSE.md").exists()


def test_activate_fails_when_base_has_advanced_since_capture(tmp_path: Path) -> None:
    overlay_store, workspace_root, source_root = _workspace_triplet(tmp_path)
    overlay_ref = OverlayRef(author="atlas", name="base-sensitive")
    _write_file(source_root / "settings.toml", 'theme = "owl"\n')

    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))
    write_workspace_allowlist(workspace_root, [{"kind": "path", "pattern": "atlas/*", "trust_class": "local"}])
    _write_file(workspace_root / ".grip" / "overlay-base-state.toml", 'advanced = true\n')

    with pytest.raises(OverlayActivationError) as exc:
        activate_overlay(
            workspace_root=workspace_root,
            overlay_store=overlay_store,
            overlay_ref=overlay_ref,
            overlay_source_kind="path",
            overlay_source_value="atlas/base-sensitive",
            overlay_signer=None,
        )

    assert exc.value.error_code == "base_advanced"


def test_activate_reports_composition_conflict_when_curated_merge_cannot_resolve(
    tmp_path: Path,
) -> None:
    overlay_store, workspace_root, source_root = _workspace_triplet(tmp_path)
    overlay_ref = OverlayRef(author="atlas", name="conflict-pack")
    _write_file(source_root / "settings.toml", 'theme = "overlay"\n')
    _write_file(workspace_root / "settings.toml", 'theme = "base"\n')
    _write_file(workspace_root / ".gitattributes", "*.toml merge=overlay-deep\n")
    _write_file(workspace_root / ".grip" / "force-conflict.toml", 'enabled = true\n')

    capture_overlay_object(overlay_store, source_root, _overlay_meta(overlay_ref))
    write_workspace_allowlist(workspace_root, [{"kind": "path", "pattern": "atlas/*", "trust_class": "local"}])

    with pytest.raises(OverlayActivationError) as exc:
        activate_overlay(
            workspace_root=workspace_root,
            overlay_store=overlay_store,
            overlay_ref=overlay_ref,
            overlay_source_kind="path",
            overlay_source_value="atlas/conflict-pack",
            overlay_signer=None,
        )

    assert exc.value.error_code == "composition_conflict"


def test_roundtrip_activate_on_peer_workspace_after_push_and_pull(tmp_path: Path) -> None:
    local_store = tmp_path / "local-store.git"
    remote_store = tmp_path / "remote-store.git"
    peer_store = tmp_path / "peer-store.git"
    local_store.mkdir()
    remote_store.mkdir()
    peer_store.mkdir()
    local_store = _init_bare_git_repo(local_store)
    remote_store = _init_bare_git_repo(remote_store)
    peer_store = _init_bare_git_repo(peer_store)

    source_root = tmp_path / "source"
    peer_workspace = tmp_path / "peer-workspace"
    source_root.mkdir()
    peer_workspace.mkdir()
    overlay_ref = OverlayRef(author="atlas", name="peer-activate")
    _write_file(source_root / "COMPOSE.md", "overlay compose\n")

    capture_overlay_object(local_store, source_root, _overlay_meta(overlay_ref))
    push_overlay_ref(local_store, remote_store, overlay_ref)
    fetch_overlay_ref(peer_store, remote_store, overlay_ref)
    write_workspace_allowlist(peer_workspace, [{"kind": "path", "pattern": "atlas/*", "trust_class": "local"}])

    result = activate_overlay(
        workspace_root=peer_workspace,
        overlay_store=peer_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/peer-activate",
        overlay_signer=None,
    )

    assert result.status == "ok"
    assert (peer_workspace / "COMPOSE.md").read_text() == "overlay compose\n"


def _workspace_triplet(tmp_path: Path) -> tuple[Path, Path, Path]:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = tmp_path / "workspace"
    source_root = tmp_path / "overlay-source"
    workspace_root.mkdir()
    source_root.mkdir()
    return overlay_store, workspace_root, source_root


def _overlay_meta(overlay_ref: OverlayRef, trust: TrustLevel = TrustLevel.TRUSTED) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=trust,
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


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

"""M1 acceptance harness: config overlay end-to-end roundtrip.

Exercises the full substrate flow:
  capture -> push -> drop local ref -> fetch -> activate -> verify -> deactivate -> verify clean
"""

from __future__ import annotations

import subprocess
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

SOURCE_FILES: dict[str, str] = {
    "COMPOSE.md": "# Overlay Compose\n\nTheme: dark-owl\n",
    "settings.toml": (
        '[ui]\ntheme = "owl"\naccent = "teal"\n\n[agent]\nname = "atlas"\nrole = "reviewer"\n'
    ),
    "config/defaults.yml": ("logging:\n  level: info\n  format: json\nretry:\n  max_attempts: 3\n"),
    "config/overrides.json": (
        '{\n  "feature_flags": {\n    "overlay_v2": true,\n    "dark_mode": true\n  }\n}\n'
    ),
}


def test_full_roundtrip_capture_push_drop_fetch_activate_verify_deactivate(
    tmp_path: Path,
) -> None:
    machine_a_store = _init_bare_git_repo(tmp_path / "machine-a.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote.git")
    machine_b_store = _init_bare_git_repo(tmp_path / "machine-b.git")

    source_root = tmp_path / "source"
    machine_b_workspace = tmp_path / "machine-b-workspace"
    source_root.mkdir()
    machine_b_workspace.mkdir()

    overlay_ref = OverlayRef(author="atlas", name="theme-dark-owl")

    for rel_path, content in SOURCE_FILES.items():
        _write_file(source_root / rel_path, content)

    capture_overlay_object(machine_a_store, source_root, _overlay_meta(overlay_ref))
    push_overlay_ref(machine_a_store, remote_store, overlay_ref)

    _drop_ref(machine_a_store, overlay_ref.ref_path)
    refs_after_drop = _list_refs(machine_a_store)
    assert overlay_ref.ref_path not in refs_after_drop

    fetch_overlay_ref(machine_b_store, remote_store, overlay_ref)

    result = activate_overlay(
        workspace_root=machine_b_workspace,
        overlay_store=machine_b_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/theme-dark-owl",
        overlay_signer=None,
    )

    assert result.status == "ok"
    assert result.completed == ["overlay.activated"]

    for rel_path, expected_content in SOURCE_FILES.items():
        target = machine_b_workspace / rel_path
        assert target.exists(), f"Expected file not materialized: {rel_path}"
        assert target.read_text() == expected_content, f"Content mismatch: {rel_path}"

    stack = read_active_overlay_stack(machine_b_workspace)
    assert stack == [overlay_ref.ref_path]

    deactivated = deactivate_overlay(
        workspace_root=machine_b_workspace,
        overlay_ref=overlay_ref,
    )

    assert deactivated.completed == ["overlay.deactivated"]

    remaining = _snapshot(machine_b_workspace)
    assert remaining == {}, f"Files remain after deactivate: {list(remaining.keys())}"

    assert read_active_overlay_stack(machine_b_workspace) == []


def test_roundtrip_with_trust_gating_blocks_then_allows_after_allowlist(
    tmp_path: Path,
) -> None:
    local_store = _init_bare_git_repo(tmp_path / "local.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote.git")
    peer_store = _init_bare_git_repo(tmp_path / "peer.git")

    source_root = tmp_path / "source"
    peer_workspace = tmp_path / "peer-workspace"
    source_root.mkdir()
    peer_workspace.mkdir()

    overlay_ref = OverlayRef(author="vendor", name="enterprise-theme")
    _write_file(source_root / "settings.toml", 'theme = "enterprise"\n')
    _write_file(source_root / "COMPOSE.md", "enterprise overlay\n")

    capture_overlay_object(local_store, source_root, _overlay_meta(overlay_ref))
    push_overlay_ref(local_store, remote_store, overlay_ref)
    fetch_overlay_ref(peer_store, remote_store, overlay_ref)

    write_workspace_allowlist(peer_workspace, [])

    with pytest.raises(OverlayActivationError) as exc:
        activate_overlay(
            workspace_root=peer_workspace,
            overlay_store=peer_store,
            overlay_ref=overlay_ref,
            overlay_source_kind="path",
            overlay_source_value="vendor/enterprise-theme",
            overlay_signer=None,
        )

    assert exc.value.error_code == "overlay_untrusted"
    assert not (peer_workspace / "settings.toml").exists()

    write_workspace_allowlist(
        peer_workspace,
        [{"kind": "path", "pattern": "vendor/*", "trust_class": "team"}],
    )

    result = activate_overlay(
        workspace_root=peer_workspace,
        overlay_store=peer_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="vendor/enterprise-theme",
        overlay_signer=None,
    )

    assert result.status == "ok"
    assert (peer_workspace / "settings.toml").read_text() == 'theme = "enterprise"\n'
    assert (peer_workspace / "COMPOSE.md").read_text() == "enterprise overlay\n"


def test_roundtrip_open_mode_without_trust_config(tmp_path: Path) -> None:
    local_store = _init_bare_git_repo(tmp_path / "local.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote.git")
    peer_store = _init_bare_git_repo(tmp_path / "peer.git")

    source_root = tmp_path / "source"
    peer_workspace = tmp_path / "peer-workspace"
    source_root.mkdir()
    peer_workspace.mkdir()

    overlay_ref = OverlayRef(author="community", name="open-theme")
    _write_file(source_root / "settings.toml", 'theme = "community"\n')

    capture_overlay_object(local_store, source_root, _overlay_meta(overlay_ref))
    push_overlay_ref(local_store, remote_store, overlay_ref)
    fetch_overlay_ref(peer_store, remote_store, overlay_ref)

    result = activate_overlay(
        workspace_root=peer_workspace,
        overlay_store=peer_store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="community/open-theme",
        overlay_signer=None,
    )

    assert result.status == "ok"
    assert (peer_workspace / "settings.toml").read_text() == 'theme = "community"\n'


def test_idempotent_activate_deactivate_cycle_preserves_non_overlay_files(
    tmp_path: Path,
) -> None:
    store = _init_bare_git_repo(tmp_path / "store.git")
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    _write_file(workspace / "my-notes.txt", "user notes\n")
    _write_file(workspace / "config/local.toml", "local = true\n")

    overlay_ref = OverlayRef(author="atlas", name="non-destructive")
    _write_file(source / "settings.toml", "overlay = true\n")
    _write_file(source / "COMPOSE.md", "compose\n")

    capture_overlay_object(store, source, _overlay_meta(overlay_ref))

    pre_snapshot = _snapshot(workspace)

    activate_overlay(
        workspace_root=workspace,
        overlay_store=store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/non-destructive",
        overlay_signer=None,
    )

    assert (workspace / "settings.toml").exists()
    assert (workspace / "my-notes.txt").read_text() == "user notes\n"
    assert (workspace / "config/local.toml").read_text() == "local = true\n"

    deactivate_overlay(workspace_root=workspace, overlay_ref=overlay_ref)

    post_snapshot = _snapshot(workspace)
    assert post_snapshot == pre_snapshot


def test_activate_after_deactivate_produces_identical_state(tmp_path: Path) -> None:
    store = _init_bare_git_repo(tmp_path / "store.git")
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    overlay_ref = OverlayRef(author="atlas", name="idempotent-check")
    for rel_path, content in SOURCE_FILES.items():
        _write_file(source / rel_path, content)

    capture_overlay_object(store, source, _overlay_meta(overlay_ref))

    activate_overlay(
        workspace_root=workspace,
        overlay_store=store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/idempotent-check",
        overlay_signer=None,
    )
    first_snapshot = _snapshot(workspace)

    deactivate_overlay(workspace_root=workspace, overlay_ref=overlay_ref)

    activate_overlay(
        workspace_root=workspace,
        overlay_store=store,
        overlay_ref=overlay_ref,
        overlay_source_kind="path",
        overlay_source_value="atlas/idempotent-check",
        overlay_signer=None,
    )
    second_snapshot = _snapshot(workspace)

    assert second_snapshot == first_snapshot


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
        if path.is_file() and ".grip" not in path.parts
    }


def _drop_ref(git_dir: Path, ref: str) -> None:
    subprocess.run(
        ["git", f"--git-dir={git_dir}", "update-ref", "-d", ref],
        check=True,
        capture_output=True,
        text=True,
    )


def _list_refs(git_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "for-each-ref", "--format=%(refname)"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().splitlines()

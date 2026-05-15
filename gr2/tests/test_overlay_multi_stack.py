"""TDD spec: multi-overlay stack composition.

Tests that activate.py supports stacking multiple overlays with:
- Append-not-replace activation (second overlay preserves first)
- Partial deactivation (remove one overlay, keep others)
- Independent managed-files tracking per overlay
- Idempotent re-activation within a stack
- Clean teardown when last overlay deactivated
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2_overlay.activate import (
    ActivationResult,
    DeactivationResult,
    activate_overlay,
    deactivate_overlay,
    read_active_overlay_stack,
    _load_managed_files,
)
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.trust import write_workspace_allowlist
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def _init_bare_git_repo(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _overlay_meta(overlay_ref: OverlayRef) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author=overlay_ref.author,
        signature="unsigned",
        timestamp="2026-05-15T00:00:00Z",
        parent_overlay_refs=[],
    )


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def _setup_two_overlays(tmp_path: Path) -> tuple[Path, Path, OverlayRef, OverlayRef]:
    """Create workspace with two distinct overlays captured in the store.

    Overlay A: alice/base-config with agents.toml
    Overlay B: bob/theme-dark with theme.toml
    """
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    source_a = tmp_path / "source-a"
    source_a.mkdir()
    _write_file(source_a / "agents.toml", 'name = "base"\n')

    source_b = tmp_path / "source-b"
    source_b.mkdir()
    _write_file(source_b / "theme.toml", 'theme = "dark"\n')

    ref_a = OverlayRef(author="alice", name="base-config")
    ref_b = OverlayRef(author="bob", name="theme-dark")

    capture_overlay_object(overlay_store, source_a, _overlay_meta(ref_a))
    capture_overlay_object(overlay_store, source_b, _overlay_meta(ref_b))

    write_workspace_allowlist(
        workspace_root,
        [
            {"kind": "path", "pattern": "alice/*", "trust_class": "local"},
            {"kind": "path", "pattern": "bob/*", "trust_class": "local"},
        ],
    )

    return overlay_store, workspace_root, ref_a, ref_b


def _activate(workspace_root: Path, overlay_store: Path, ref: OverlayRef) -> ActivationResult:
    return activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=ref,
        overlay_source_kind="path",
        overlay_source_value=f"{ref.author}/{ref.name}",
        overlay_signer=None,
    )


class TestMultiOverlayActivation:
    def test_second_activation_preserves_first_on_stack(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        stack = read_active_overlay_stack(workspace_root)
        assert ref_a.ref_path in stack
        assert ref_b.ref_path in stack
        assert len(stack) == 2

    def test_second_activation_preserves_first_overlay_files(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        assert (workspace_root / "agents.toml").exists()

        _activate(workspace_root, overlay_store, ref_b)
        assert (workspace_root / "agents.toml").exists(), "First overlay's files destroyed by second activation"
        assert (workspace_root / "theme.toml").exists()

    def test_second_activation_returns_ok(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        result_a = _activate(workspace_root, overlay_store, ref_a)
        result_b = _activate(workspace_root, overlay_store, ref_b)

        assert result_a.status == "ok"
        assert result_b.status == "ok"

    def test_idempotent_reactivation_no_duplicate_stack_entry(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        _activate(workspace_root, overlay_store, ref_a)

        stack = read_active_overlay_stack(workspace_root)
        assert stack.count(ref_a.ref_path) == 1, "Re-activation created duplicate stack entry"
        assert len(stack) == 2

    def test_stack_order_is_activation_order(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        stack = read_active_overlay_stack(workspace_root)
        assert stack[0] == ref_a.ref_path
        assert stack[1] == ref_b.ref_path


class TestPartialDeactivation:
    def test_deactivate_one_keeps_other_on_stack(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_b)

        stack = read_active_overlay_stack(workspace_root)
        assert stack == [ref_a.ref_path]

    def test_deactivate_one_removes_only_its_files(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_b)

        assert (workspace_root / "agents.toml").exists(), "Overlay A's files removed during B's deactivation"
        assert not (workspace_root / "theme.toml").exists(), "Overlay B's files not removed"

    def test_deactivate_one_preserves_other_managed_files(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_b)

        managed = _load_managed_files(workspace_root)
        assert ref_a.ref_path in managed
        assert ref_b.ref_path not in managed

    def test_deactivate_last_overlay_empties_stack(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_a)

        stack = read_active_overlay_stack(workspace_root)
        assert stack == []

    def test_deactivate_all_sequentially(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)
        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_b)
        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_a)

        stack = read_active_overlay_stack(workspace_root)
        assert stack == []
        assert not (workspace_root / "agents.toml").exists()
        assert not (workspace_root / "theme.toml").exists()

    def test_deactivate_nonexistent_overlay_is_noop(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        phantom = OverlayRef(author="nobody", name="phantom")
        result = deactivate_overlay(workspace_root=workspace_root, overlay_ref=phantom)

        assert result.completed == ["overlay.deactivated"]
        stack = read_active_overlay_stack(workspace_root)
        assert stack == [ref_a.ref_path]


class TestManagedFilesTracking:
    def test_managed_files_tracked_per_overlay(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        managed = _load_managed_files(workspace_root)
        assert ref_a.ref_path in managed
        assert ref_b.ref_path in managed
        assert "agents.toml" in managed[ref_a.ref_path]
        assert "theme.toml" in managed[ref_b.ref_path]

    def test_managed_files_not_cross_contaminated(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, ref_b = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        _activate(workspace_root, overlay_store, ref_b)

        managed = _load_managed_files(workspace_root)
        assert "theme.toml" not in managed[ref_a.ref_path]
        assert "agents.toml" not in managed[ref_b.ref_path]


class TestExistingTestsStillPass:
    """Verify that single-overlay behavior (the existing contract) isn't broken."""

    def test_single_activate_deactivate_still_works(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, _ = _setup_two_overlays(tmp_path)

        result = _activate(workspace_root, overlay_store, ref_a)
        assert result.status == "ok"
        assert (workspace_root / "agents.toml").exists()

        deactivate_overlay(workspace_root=workspace_root, overlay_ref=ref_a)
        assert not (workspace_root / "agents.toml").exists()
        assert read_active_overlay_stack(workspace_root) == []

    def test_single_activate_idempotent(self, tmp_path: Path) -> None:
        overlay_store, workspace_root, ref_a, _ = _setup_two_overlays(tmp_path)

        _activate(workspace_root, overlay_store, ref_a)
        first_content = (workspace_root / "agents.toml").read_text()

        _activate(workspace_root, overlay_store, ref_a)
        second_content = (workspace_root / "agents.toml").read_text()

        assert first_content == second_content
        stack = read_active_overlay_stack(workspace_root)
        assert stack == [ref_a.ref_path]

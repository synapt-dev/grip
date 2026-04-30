from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2_overlay.activate import read_active_overlay_stack
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.trust import write_workspace_allowlist
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_activate_overlays_atomically_applies_to_both_repos_or_none(tmp_path: Path) -> None:
    from gr2_overlay.cross_repo import RepoOverlayTarget, activate_overlays_atomically

    overlay_ref = OverlayRef(author="atlas", name="workspace-bundle")
    app_store, app_workspace, app_source = _triplet(tmp_path, "app")
    docs_store, docs_workspace, docs_source = _triplet(tmp_path, "docs")

    _write_file(app_source / "settings.toml", 'theme = "owl"\n')
    _write_file(docs_source / "COMPOSE.md", "# Docs overlay\n")

    capture_overlay_object(app_store, app_source, _overlay_meta(overlay_ref))
    capture_overlay_object(docs_store, docs_source, _overlay_meta(overlay_ref))
    write_workspace_allowlist(
        app_workspace,
        [{"kind": "path", "pattern": "atlas/*", "trust_class": "team"}],
    )
    write_workspace_allowlist(
        docs_workspace,
        [{"kind": "path", "pattern": "atlas/*", "trust_class": "team"}],
    )

    result = activate_overlays_atomically(
        targets=[
            RepoOverlayTarget(
                repo_name="app",
                checkout_root=app_workspace,
                overlay_store=app_store,
                overlay_ref=overlay_ref,
                overlay_source_kind="path",
                overlay_source_value="atlas/workspace-bundle",
                overlay_signer=None,
            ),
            RepoOverlayTarget(
                repo_name="docs",
                checkout_root=docs_workspace,
                overlay_store=docs_store,
                overlay_ref=overlay_ref,
                overlay_source_kind="path",
                overlay_source_value="atlas/workspace-bundle",
                overlay_signer=None,
            ),
        ]
    )

    assert result.status == "ok"
    assert result.completed_repos == ["app", "docs"]
    assert result.rolled_back_repos == []
    assert (app_workspace / "settings.toml").read_text() == 'theme = "owl"\n'
    assert (docs_workspace / "COMPOSE.md").read_text() == "# Docs overlay\n"
    assert read_active_overlay_stack(app_workspace) == [overlay_ref.ref_path]
    assert read_active_overlay_stack(docs_workspace) == [overlay_ref.ref_path]


def test_atomic_apply_rolls_back_first_repo_when_second_repo_blocks(tmp_path: Path) -> None:
    from gr2_overlay.cross_repo import (
        CrossRepoActivationError,
        RepoOverlayTarget,
        activate_overlays_atomically,
    )

    overlay_ref = OverlayRef(author="atlas", name="workspace-bundle")
    app_store, app_workspace, app_source = _triplet(tmp_path, "app")
    docs_store, docs_workspace, docs_source = _triplet(tmp_path, "docs")

    _write_file(app_workspace / "keep.txt", "keep me\n")
    _write_file(app_source / "settings.toml", 'theme = "owl"\n')
    _write_file(docs_source / "COMPOSE.md", "# Docs overlay\n")

    capture_overlay_object(app_store, app_source, _overlay_meta(overlay_ref))
    capture_overlay_object(docs_store, docs_source, _overlay_meta(overlay_ref))
    write_workspace_allowlist(
        app_workspace,
        [{"kind": "path", "pattern": "atlas/*", "trust_class": "team"}],
    )
    write_workspace_allowlist(
        docs_workspace,
        [{"kind": "path", "pattern": "atlas/*", "trust_class": "team"}],
    )
    _write_file(docs_workspace / ".grip" / "overlay-base-state.toml", "advanced = true\n")

    app_before = _snapshot(app_workspace)
    docs_before = _snapshot(docs_workspace)

    with pytest.raises(CrossRepoActivationError) as exc:
        activate_overlays_atomically(
            targets=[
                RepoOverlayTarget(
                    repo_name="app",
                    checkout_root=app_workspace,
                    overlay_store=app_store,
                    overlay_ref=overlay_ref,
                    overlay_source_kind="path",
                    overlay_source_value="atlas/workspace-bundle",
                    overlay_signer=None,
                ),
                RepoOverlayTarget(
                    repo_name="docs",
                    checkout_root=docs_workspace,
                    overlay_store=docs_store,
                    overlay_ref=overlay_ref,
                    overlay_source_kind="path",
                    overlay_source_value="atlas/workspace-bundle",
                    overlay_signer=None,
                ),
            ]
        )

    assert exc.value.error_code == "base_advanced"
    assert exc.value.failing_repo == "docs"
    assert exc.value.rolled_back_repos == ["app"]
    assert _snapshot(app_workspace) == app_before
    assert _snapshot(docs_workspace) == docs_before
    assert read_active_overlay_stack(app_workspace) == []
    assert read_active_overlay_stack(docs_workspace) == []


def _triplet(tmp_path: Path, repo_name: str) -> tuple[Path, Path, Path]:
    overlay_store = _init_bare_git_repo(tmp_path / f"{repo_name}-overlay-store.git")
    checkout_root = tmp_path / repo_name
    overlay_source = tmp_path / f"{repo_name}-overlay-source"
    checkout_root.mkdir()
    overlay_source.mkdir()
    return overlay_store, checkout_root, overlay_source


def _overlay_meta(overlay_ref: OverlayRef) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=[],
    )


def _init_bare_git_repo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(path.parent, "init", "--bare", path.name)
    return path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text()
    return snapshot

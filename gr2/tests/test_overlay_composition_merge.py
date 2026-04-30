from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.trust import write_workspace_allowlist
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_reapplying_same_overlay_stack_is_deterministic_across_remerge(tmp_path: Path) -> None:
    from gr2_overlay.cross_repo import RepoOverlayTarget, activate_overlays_atomically

    base_ref = OverlayRef(author="atlas", name="base-theme")
    feature_ref = OverlayRef(author="atlas", name="feature-theme")

    app_store, app_workspace, app_source = _triplet(tmp_path, "app")
    docs_store, docs_workspace, docs_source = _triplet(tmp_path, "docs")

    _write_file(app_source / "settings.toml", '[ui]\ntheme = "base"\n')
    _write_file(docs_source / "settings.toml", '[ui]\ntheme = "base"\n')
    capture_overlay_object(app_store, app_source, _overlay_meta(base_ref))
    capture_overlay_object(docs_store, docs_source, _overlay_meta(base_ref))

    _write_file(app_source / "settings.toml", '[ui]\ntheme = "feature"\n')
    _write_file(docs_source / "settings.toml", '[ui]\ntheme = "feature"\n')
    capture_overlay_object(
        app_store,
        app_source,
        _overlay_meta(feature_ref, parents=[base_ref.ref_path]),
    )
    capture_overlay_object(
        docs_store,
        docs_source,
        _overlay_meta(feature_ref, parents=[base_ref.ref_path]),
    )

    for workspace in (app_workspace, docs_workspace):
        write_workspace_allowlist(
            workspace,
            [{"kind": "path", "pattern": "atlas/*", "trust_class": "team"}],
        )

    targets = [
        RepoOverlayTarget(
            repo_name="app",
            checkout_root=app_workspace,
            overlay_store=app_store,
            overlay_ref=feature_ref,
            overlay_source_kind="path",
            overlay_source_value="atlas/feature-theme",
            overlay_signer=None,
        ),
        RepoOverlayTarget(
            repo_name="docs",
            checkout_root=docs_workspace,
            overlay_store=docs_store,
            overlay_ref=feature_ref,
            overlay_source_kind="path",
            overlay_source_value="atlas/feature-theme",
            overlay_signer=None,
        ),
    ]

    first = activate_overlays_atomically(targets=targets)
    first_app = _snapshot(app_workspace)
    first_docs = _snapshot(docs_workspace)

    second = activate_overlays_atomically(targets=targets)
    second_app = _snapshot(app_workspace)
    second_docs = _snapshot(docs_workspace)

    assert first.status == "ok"
    assert second.status == "ok"
    assert first.completed_repos == ["app", "docs"]
    assert second.completed_repos == ["app", "docs"]
    assert second_app == first_app
    assert second_docs == first_docs


def test_cross_repo_composition_conflict_is_explicit_and_non_partial(tmp_path: Path) -> None:
    from gr2_overlay.cross_repo import (
        CrossRepoActivationError,
        RepoOverlayTarget,
        activate_overlays_atomically,
    )

    base_ref = OverlayRef(author="atlas", name="base-theme")
    feature_ref = OverlayRef(author="atlas", name="feature-theme")

    app_store, app_workspace, app_source = _triplet(tmp_path, "app")
    docs_store, docs_workspace, docs_source = _triplet(tmp_path, "docs")

    _write_file(app_source / "settings.toml", '[ui]\ntheme = "base"\n')
    _write_file(docs_source / "settings.toml", '[ui]\ntheme = "base"\n')
    capture_overlay_object(app_store, app_source, _overlay_meta(base_ref))
    capture_overlay_object(docs_store, docs_source, _overlay_meta(base_ref))

    _write_file(app_source / "settings.toml", '[ui]\ntheme = "feature"\n')
    _write_file(docs_source / "settings.toml", '[ui]\ntheme = "feature"\n')
    capture_overlay_object(
        app_store,
        app_source,
        _overlay_meta(feature_ref, parents=[base_ref.ref_path]),
    )
    capture_overlay_object(
        docs_store,
        docs_source,
        _overlay_meta(feature_ref, parents=[base_ref.ref_path]),
    )

    _write_file(docs_workspace / ".gitattributes", "*.toml merge=overlay-deep\n")
    _write_file(docs_workspace / ".grip" / "force-conflict.toml", "enabled = true\n")

    for workspace in (app_workspace, docs_workspace):
        write_workspace_allowlist(
            workspace,
            [{"kind": "path", "pattern": "atlas/*", "trust_class": "team"}],
        )

    app_before = _snapshot(app_workspace)
    docs_before = _snapshot(docs_workspace)

    with pytest.raises(CrossRepoActivationError) as exc:
        activate_overlays_atomically(
            targets=[
                RepoOverlayTarget(
                    repo_name="app",
                    checkout_root=app_workspace,
                    overlay_store=app_store,
                    overlay_ref=feature_ref,
                    overlay_source_kind="path",
                    overlay_source_value="atlas/feature-theme",
                    overlay_signer=None,
                ),
                RepoOverlayTarget(
                    repo_name="docs",
                    checkout_root=docs_workspace,
                    overlay_store=docs_store,
                    overlay_ref=feature_ref,
                    overlay_source_kind="path",
                    overlay_source_value="atlas/feature-theme",
                    overlay_signer=None,
                ),
            ]
        )

    assert exc.value.error_code == "composition_conflict"
    assert exc.value.failing_repo == "docs"
    assert _snapshot(app_workspace) == app_before
    assert _snapshot(docs_workspace) == docs_before


def _triplet(tmp_path: Path, repo_name: str) -> tuple[Path, Path, Path]:
    overlay_store = _init_bare_git_repo(tmp_path / f"{repo_name}-overlay-store.git")
    checkout_root = tmp_path / repo_name
    overlay_source = tmp_path / f"{repo_name}-overlay-source"
    checkout_root.mkdir()
    overlay_source.mkdir()
    return overlay_store, checkout_root, overlay_source


def _overlay_meta(overlay_ref: OverlayRef, parents: list[str] | None = None) -> OverlayMeta:
    return OverlayMeta(
        ref=overlay_ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=parents or [],
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

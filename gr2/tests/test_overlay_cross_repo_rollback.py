from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2_overlay.activate import OverlayActivationError, activate_overlay, deactivate_overlay
from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.trust import write_workspace_allowlist
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_rollback_restores_every_touched_repo_after_partial_second_repo_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.cross_repo as cross_repo

    overlay_ref = OverlayRef(author="atlas", name="workspace-bundle")
    app_store, app_workspace, app_source = _triplet(tmp_path, "app")
    docs_store, docs_workspace, docs_source = _triplet(tmp_path, "docs")

    _write_file(app_workspace / "keep.txt", "keep me\n")
    _write_file(docs_workspace / "README.md", "docs base\n")
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

    before_app = _snapshot(app_workspace)
    before_docs = _snapshot(docs_workspace)

    real_activate = activate_overlay

    def fake_activate_overlay(*, workspace_root: Path, **kwargs):
        if workspace_root == docs_workspace:
            _write_file(docs_workspace / "COMPOSE.md", "# partially written\n")
            _write_file(docs_workspace / ".grip" / "overlay-stack.json", '["corrupt"]\n')
            raise OverlayActivationError("simulated docs failure", error_code="composition_conflict")
        return real_activate(workspace_root=workspace_root, **kwargs)

    monkeypatch.setattr(cross_repo, "activate_overlay", fake_activate_overlay)
    monkeypatch.setattr(cross_repo, "deactivate_overlay", deactivate_overlay)

    with pytest.raises(cross_repo.CrossRepoActivationError) as exc:
        cross_repo.activate_overlays_atomically(
            targets=[
                cross_repo.RepoOverlayTarget(
                    repo_name="app",
                    checkout_root=app_workspace,
                    overlay_store=app_store,
                    overlay_ref=overlay_ref,
                    overlay_source_kind="path",
                    overlay_source_value="atlas/workspace-bundle",
                    overlay_signer=None,
                ),
                cross_repo.RepoOverlayTarget(
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

    assert exc.value.error_code == "composition_conflict"
    assert exc.value.failing_repo == "docs"
    assert exc.value.rolled_back_repos == ["app", "docs"]
    assert _snapshot(app_workspace) == before_app
    assert _snapshot(docs_workspace) == before_docs


def test_rollback_does_not_leave_cross_repo_transaction_artifacts_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gr2_overlay.cross_repo as cross_repo

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

    real_activate = activate_overlay

    def fake_activate_overlay(*, workspace_root: Path, **kwargs):
        if workspace_root == docs_workspace:
            _write_file(docs_workspace / ".grip" / "cross-repo-transaction.json", '{"status":"partial"}\n')
            raise OverlayActivationError("simulated docs failure", error_code="base_advanced")
        return real_activate(workspace_root=workspace_root, **kwargs)

    monkeypatch.setattr(cross_repo, "activate_overlay", fake_activate_overlay)
    monkeypatch.setattr(cross_repo, "deactivate_overlay", deactivate_overlay)

    with pytest.raises(cross_repo.CrossRepoActivationError):
        cross_repo.activate_overlays_atomically(
            targets=[
                cross_repo.RepoOverlayTarget(
                    repo_name="app",
                    checkout_root=app_workspace,
                    overlay_store=app_store,
                    overlay_ref=overlay_ref,
                    overlay_source_kind="path",
                    overlay_source_value="atlas/workspace-bundle",
                    overlay_signer=None,
                ),
                cross_repo.RepoOverlayTarget(
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

    assert not any(app_workspace.rglob("cross-repo-transaction.json"))
    assert not any(docs_workspace.rglob("cross-repo-transaction.json"))


def test_snapshot_handles_binary_files_without_crashing(tmp_path: Path) -> None:
    from gr2_overlay.cross_repo import _restore_snapshot, _snapshot

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "readme.txt").write_text("hello")
    binary_dir = root / ".grip" / "overlays"
    binary_dir.mkdir(parents=True)
    (binary_dir / "pack.idx").write_bytes(b"\x00\xff\xfe\x80\x90")

    snap = _snapshot(root)
    assert snap["readme.txt"] == b"hello"
    assert snap[".grip/overlays/pack.idx"] == b"\x00\xff\xfe\x80\x90"

    (root / "readme.txt").write_text("mutated")
    (root / "extra.txt").write_text("added")

    _restore_snapshot(root, snap)

    assert (root / "readme.txt").read_bytes() == b"hello"
    assert not (root / "extra.txt").exists()
    assert (binary_dir / "pack.idx").read_bytes() == b"\x00\xff\xfe\x80\x90"


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

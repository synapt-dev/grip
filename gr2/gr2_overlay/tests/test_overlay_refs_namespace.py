from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.objects import capture_overlay_object
from gr2_overlay.refs import fetch_overlay_ref, push_overlay_ref
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_capture_creates_overlay_ref_in_tier_a_namespace(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = tmp_path / "overlay-source"
    source_root.mkdir()

    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "settings.toml", 'theme = "owl"\n')
    _write_file(source_root / "skills" / "ci.yml", "steps:\n  - lint\n")
    _write_file(source_root / "prompts" / "review.json", '{\n  "prompt": "be precise"\n}\n')
    _write_file(source_root / "ignored.py", "print('not tier a')\n")

    ref = OverlayRef(author="atlas", name="theme-dark")
    metadata = OverlayMeta(
        ref=ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=[],
    )

    capture_overlay_object(
        overlay_store=overlay_store,
        source_root=source_root,
        metadata=metadata,
    )

    tag_oid = _git_output(overlay_store, "rev-parse", ref.ref_path)
    assert _git_output(overlay_store, "cat-file", "-t", tag_oid) == "tag"
    assert ref.ref_path == "refs/overlays/atlas/theme-dark"


def test_push_copies_same_overlay_ref_hash_to_remote_store(tmp_path: Path) -> None:
    local_store = _init_bare_git_repo(tmp_path / "local-overlay-store.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote-overlay-store.git")
    source_root = tmp_path / "overlay-source"
    source_root.mkdir()

    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "agents.toml", 'name = "atlas"\n')

    ref = OverlayRef(author="atlas", name="shared-base")
    metadata = OverlayMeta(
        ref=ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=[],
    )

    capture_overlay_object(
        overlay_store=local_store,
        source_root=source_root,
        metadata=metadata,
    )

    local_tag_oid = _git_output(local_store, "rev-parse", ref.ref_path)

    push_overlay_ref(
        overlay_store=local_store,
        remote_store=remote_store,
        overlay_ref=ref,
    )

    remote_tag_oid = _git_output(remote_store, "rev-parse", ref.ref_path)
    assert remote_tag_oid == local_tag_oid


def test_fetch_restores_dropped_local_overlay_ref_with_same_hash(tmp_path: Path) -> None:
    local_store = _init_bare_git_repo(tmp_path / "local-overlay-store.git")
    remote_store = _init_bare_git_repo(tmp_path / "remote-overlay-store.git")
    source_root = tmp_path / "overlay-source"
    source_root.mkdir()

    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "skills" / "ci.yml", "steps:\n  - lint\n")

    ref = OverlayRef(author="atlas", name="review-defaults")
    metadata = OverlayMeta(
        ref=ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=["refs/overlays/team/shared-base"],
    )

    capture_overlay_object(
        overlay_store=local_store,
        source_root=source_root,
        metadata=metadata,
    )
    original_oid = _git_output(local_store, "rev-parse", ref.ref_path)

    push_overlay_ref(
        overlay_store=local_store,
        remote_store=remote_store,
        overlay_ref=ref,
    )

    _git_run(local_store, "update-ref", "-d", ref.ref_path)
    assert not _ref_exists(local_store, ref.ref_path)

    fetch_overlay_ref(
        overlay_store=local_store,
        remote_store=remote_store,
        overlay_ref=ref,
    )

    restored_oid = _git_output(local_store, "rev-parse", ref.ref_path)
    assert restored_oid == original_oid


def _init_bare_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def _ref_exists(git_dir: Path, ref_name: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "show-ref", "--verify", "--quiet", ref_name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _git_output(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_run(git_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )

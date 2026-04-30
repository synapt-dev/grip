from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from gr2_overlay.objects import apply_overlay_object, capture_overlay_object
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_capture_serializes_attached_head_state_per_repo_in_metadata(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_attached_head_checkout(tmp_path / "attached-source")

    ref = OverlayRef(author="atlas", name="tier-b-state")
    meta = OverlayMeta(
        ref=ref,
        tier=_tier_b(),
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-02T00:00:00Z",
        parent_overlay_refs=[],
    )

    capture_overlay_object(
        overlay_store=overlay_store,
        source_root=source_root,
        metadata=meta,
    )

    tag_oid = _git_output(overlay_store, "rev-parse", ref.ref_path)
    tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")
    metadata = tomllib.loads(_git_show(overlay_store, f"{tree_oid}:metadata_blob"))

    assert metadata["tier"] == "source"
    assert metadata["repo_state"] == [
        {
            "repo": ".",
            "head_branch": "feat-auth",
            "head_ref": "refs/heads/feat-auth",
            "head_detached": False,
            "head_commit": _git_output(source_root, "rev-parse", "HEAD"),
        }
    ]


def test_capture_marks_detached_head_without_inventing_branch_names(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_detached_head_checkout(tmp_path / "detached-source")

    ref = OverlayRef(author="atlas", name="tier-b-detached")
    meta = OverlayMeta(
        ref=ref,
        tier=_tier_b(),
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-02T00:00:00Z",
        parent_overlay_refs=[],
    )

    capture_overlay_object(
        overlay_store=overlay_store,
        source_root=source_root,
        metadata=meta,
    )

    tag_oid = _git_output(overlay_store, "rev-parse", ref.ref_path)
    tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")
    metadata = tomllib.loads(_git_show(overlay_store, f"{tree_oid}:metadata_blob"))

    assert metadata["repo_state"] == [
        {
            "repo": ".",
            "head_branch": "",
            "head_ref": "",
            "head_detached": True,
            "head_commit": _git_output(source_root, "rev-parse", "HEAD"),
        }
    ]


def test_apply_preserves_target_head_state_and_treats_repo_state_as_provenance(
    tmp_path: Path,
) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_attached_head_checkout(tmp_path / "attached-source")
    target_root = _init_clean_target_checkout(tmp_path / "clean-target")

    ref = OverlayRef(author="atlas", name="tier-b-state")
    meta = OverlayMeta(
        ref=ref,
        tier=_tier_b(),
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-02T00:00:00Z",
        parent_overlay_refs=[],
    )

    starting_head = _git_output(target_root, "symbolic-ref", "HEAD")
    starting_commit = _git_output(target_root, "rev-parse", "HEAD")

    capture_overlay_object(
        overlay_store=overlay_store,
        source_root=source_root,
        metadata=meta,
    )
    apply_overlay_object(
        overlay_store=overlay_store,
        overlay_ref=ref,
        checkout_root=target_root,
    )

    assert _git_output(target_root, "symbolic-ref", "HEAD") == starting_head
    assert _git_output(target_root, "rev-parse", "HEAD") == starting_commit
    assert (target_root / "src" / "auth.py").read_text() == "ROLE = 'overlay-working'\n"
    assert _git_index_blob(target_root, "src/auth.py") == "ROLE = 'overlay-staged'\n"


def _tier_b() -> OverlayTier:
    return getattr(OverlayTier, "B")


def _init_attached_head_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Atlas")
    _git(path, "config", "user.email", "atlas@example.com")

    _write_file(path / "src" / "auth.py", "ROLE = 'base'\n")
    _git(path, "add", "src/auth.py")
    _git(path, "commit", "-m", "base")

    _git(path, "checkout", "-b", "feat-auth")
    _write_file(path / "src" / "auth.py", "ROLE = 'overlay-staged'\n")
    _git(path, "add", "src/auth.py")
    _write_file(path / "src" / "auth.py", "ROLE = 'overlay-working'\n")
    return path


def _init_detached_head_checkout(path: Path) -> Path:
    checkout = _init_attached_head_checkout(path)
    _git(checkout, "checkout", "--detach")
    return checkout


def _init_clean_target_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Atlas")
    _git(path, "config", "user.email", "atlas@example.com")

    _write_file(path / "src" / "auth.py", "ROLE = 'base'\n")
    _git(path, "add", "src/auth.py")
    _git(path, "commit", "-m", "base")
    return path


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


def _git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_show(repo: Path, rev: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", rev],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _git_index_blob(repo: Path, relpath: str) -> str:
    blob_oid = _git_output(repo, "ls-files", "--stage", "--", relpath).split()[1]
    return _git_show(repo, blob_oid)


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)

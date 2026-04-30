from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.objects import apply_overlay_object, capture_overlay_object
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_round_trip_preserves_exact_tracked_porcelain_status(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_mixed_tracked_checkout(tmp_path / "dirty-source")
    target_root = _init_clean_target_checkout(tmp_path / "clean-target")

    ref = OverlayRef(author="atlas", name="tier-b-discipline")
    meta = OverlayMeta(
        ref=ref,
        tier=_tier_b(),
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-02T00:00:00Z",
        parent_overlay_refs=[],
    )

    expected_status = _git_lines(source_root, "status", "--short")
    assert expected_status == [
        "MM src/auth.py",
        "A  src/new_feature.py",
        "D  src/obsolete.py",
        " M src/router.py",
    ]

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

    assert _git_lines(target_root, "status", "--short") == expected_status


def test_round_trip_does_not_promote_unstaged_lines_into_index(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_mixed_tracked_checkout(tmp_path / "dirty-source")
    target_root = _init_clean_target_checkout(tmp_path / "clean-target")

    ref = OverlayRef(author="atlas", name="tier-b-discipline")
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
    apply_overlay_object(
        overlay_store=overlay_store,
        overlay_ref=ref,
        checkout_root=target_root,
    )

    assert _git_index_blob(target_root, "src/auth.py") == "ROLE = 'staged'\n"
    assert (target_root / "src" / "auth.py").read_text() == "ROLE = 'working'\n"
    assert _git_index_blob(target_root, "src/router.py") == "ROUTE = 'base'\n"
    assert (target_root / "src" / "router.py").read_text() == "ROUTE = 'working-only'\n"
    assert not (target_root / "src" / "obsolete.py").exists()
    assert (target_root / "src" / "new_feature.py").read_text() == "ENABLED = True\n"


def _tier_b() -> OverlayTier:
    return getattr(OverlayTier, "B")


def _init_mixed_tracked_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Atlas")
    _git(path, "config", "user.email", "atlas@example.com")

    _write_file(path / "src" / "auth.py", "ROLE = 'base'\n")
    _write_file(path / "src" / "router.py", "ROUTE = 'base'\n")
    _write_file(path / "src" / "obsolete.py", "LEGACY = True\n")
    _git(path, "add", "src/auth.py", "src/router.py", "src/obsolete.py")
    _git(path, "commit", "-m", "base")

    _write_file(path / "src" / "auth.py", "ROLE = 'staged'\n")
    _git(path, "add", "src/auth.py")
    _write_file(path / "src" / "auth.py", "ROLE = 'working'\n")

    _write_file(path / "src" / "new_feature.py", "ENABLED = True\n")
    _git(path, "add", "src/new_feature.py")

    _git(path, "rm", "src/obsolete.py")

    _write_file(path / "src" / "router.py", "ROUTE = 'working-only'\n")
    return path


def _init_clean_target_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Atlas")
    _git(path, "config", "user.email", "atlas@example.com")

    _write_file(path / "src" / "auth.py", "ROLE = 'base'\n")
    _write_file(path / "src" / "router.py", "ROUTE = 'base'\n")
    _write_file(path / "src" / "obsolete.py", "LEGACY = True\n")
    _git(path, "add", "src/auth.py", "src/router.py", "src/obsolete.py")
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


def _git_lines(repo: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.rstrip("\n") for line in result.stdout.splitlines() if line.strip()]


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

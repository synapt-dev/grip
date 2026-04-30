from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.objects import apply_overlay_object, capture_overlay_object
from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_capture_writes_separate_tier_b_staged_and_working_trees_with_repo_state_metadata(
    tmp_path: Path,
) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_dirty_source_checkout(tmp_path / "dirty-source")

    ref = OverlayRef(author="atlas", name="engine-patch")
    meta = OverlayMeta(
        ref=ref,
        tier=_tier_b(),
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=["refs/overlays/team/base-engine"],
    )

    capture_overlay_object(
        overlay_store=overlay_store,
        source_root=source_root,
        metadata=meta,
    )

    tag_oid = _git_output(overlay_store, "rev-parse", ref.ref_path)
    structured_tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")

    assert _ls_tree_names(overlay_store, structured_tree_oid) == [
        "metadata_blob",
        "staged_index_tree",
        "untracked_blobs",
        "working_tree_tree",
    ]

    metadata_blob = _git_show(overlay_store, f"{structured_tree_oid}:metadata_blob")
    assert 'tier = "source"' in metadata_blob
    assert 'head_branch = "main"' in metadata_blob
    assert 'head_ref = "refs/heads/main"' in metadata_blob
    assert 'head_commit = "' in metadata_blob
    assert "refs/overlays/team/base-engine" in metadata_blob

    working_tree_oid = _tree_entry_oid(overlay_store, structured_tree_oid, "working_tree_tree")
    staged_tree_oid = _tree_entry_oid(overlay_store, structured_tree_oid, "staged_index_tree")
    untracked_tree_oid = _tree_entry_oid(overlay_store, structured_tree_oid, "untracked_blobs")

    expected_files = [
        "pkg/engine.py",
        "src/lib.rs",
        "web/client.ts",
    ]
    assert _flatten_tree(overlay_store, working_tree_oid) == expected_files
    assert _flatten_tree(overlay_store, staged_tree_oid) == expected_files
    assert _flatten_tree(overlay_store, untracked_tree_oid) == []

    working_app = _git_show(overlay_store, f"{working_tree_oid}:pkg/engine.py")
    staged_app = _git_show(overlay_store, f"{staged_tree_oid}:pkg/engine.py")
    assert working_app == "print('working')\n"
    assert staged_app == "print('staged')\n"

    working_ts = _git_show(overlay_store, f"{working_tree_oid}:web/client.ts")
    staged_ts = _git_show(overlay_store, f"{staged_tree_oid}:web/client.ts")
    assert working_ts == "export const mode = 'working';\n"
    assert staged_ts == "export const mode = 'base';\n"


def test_apply_round_trips_tier_b_working_and_staging_state_on_clean_checkout(
    tmp_path: Path,
) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = _init_dirty_source_checkout(tmp_path / "dirty-source")
    target_root = _init_clean_target_checkout(tmp_path / "clean-target")

    ref = OverlayRef(author="atlas", name="engine-patch")
    meta = OverlayMeta(
        ref=ref,
        tier=_tier_b(),
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
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

    assert (target_root / "pkg/engine.py").read_text() == "print('working')\n"
    assert (target_root / "src/lib.rs").read_text() == "pub fn meaning() -> i32 { 43 }\n"
    assert (target_root / "web/client.ts").read_text() == "export const mode = 'working';\n"

    assert _git_index_blob(target_root, "pkg/engine.py") == "print('staged')\n"
    assert _git_index_blob(target_root, "src/lib.rs") == "pub fn meaning() -> i32 { 43 }\n"
    assert _git_index_blob(target_root, "web/client.ts") == "export const mode = 'base';\n"

    assert sorted(_git_lines(target_root, "diff", "--cached", "--name-only")) == [
        "pkg/engine.py",
        "src/lib.rs",
    ]
    assert sorted(_git_lines(target_root, "diff", "--name-only")) == [
        "pkg/engine.py",
        "web/client.ts",
    ]

    assert not (target_root / "scratch.tmp").exists()
    assert not (target_root / "vendor" / "dep").exists()


def _tier_b() -> OverlayTier:
    return getattr(OverlayTier, "B")


def _init_dirty_source_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Atlas")
    _git(path, "config", "user.email", "atlas@example.com")

    _write_file(path / "pkg/engine.py", "print('base')\n")
    _write_file(path / "src/lib.rs", "pub fn meaning() -> i32 { 42 }\n")
    _write_file(path / "web/client.ts", "export const mode = 'base';\n")
    _write_file(path / "README.md", "docs stay out of Tier B\n")

    _git(path, "add", "pkg/engine.py", "src/lib.rs", "web/client.ts", "README.md")
    _git(path, "commit", "-m", "base")

    _write_file(path / "pkg/engine.py", "print('staged')\n")
    _git(path, "add", "pkg/engine.py")
    _write_file(path / "pkg/engine.py", "print('working')\n")

    _write_file(path / "src/lib.rs", "pub fn meaning() -> i32 { 43 }\n")
    _git(path, "add", "src/lib.rs")

    _write_file(path / "web/client.ts", "export const mode = 'working';\n")
    _write_file(path / "scratch.tmp", "exclude me\n")
    _write_file(path / "vendor" / "dep", "not a tracked source file\n")
    return path


def _init_clean_target_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Atlas")
    _git(path, "config", "user.email", "atlas@example.com")

    _write_file(path / "pkg/engine.py", "print('base')\n")
    _write_file(path / "src/lib.rs", "pub fn meaning() -> i32 { 42 }\n")
    _write_file(path / "web/client.ts", "export const mode = 'base';\n")
    _write_file(path / "README.md", "docs stay out of Tier B\n")

    _git(path, "add", "pkg/engine.py", "src/lib.rs", "web/client.ts", "README.md")
    _git(path, "commit", "-m", "base")
    return path


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


def _ls_tree_names(git_dir: Path, tree_oid: str) -> list[str]:
    return sorted(_git_lines_with_git_dir(git_dir, "ls-tree", "--name-only", tree_oid))


def _tree_entry_oid(git_dir: Path, tree_oid: str, entry_name: str) -> str:
    line = _git_output(git_dir, "ls-tree", tree_oid, entry_name)
    return line.split()[2]


def _flatten_tree(git_dir: Path, tree_oid: str) -> list[str]:
    return sorted(_git_lines_with_git_dir(git_dir, "ls-tree", "-r", "--name-only", tree_oid))


def _git_show(git_dir: Path, object_spec: str) -> str:
    return _git_output(git_dir, "show", object_spec)


def _git_index_blob(repo: Path, rel_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f":{rel_path}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _git_lines(repo: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_lines_with_git_dir(git_dir: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.objects import apply_overlay_object, capture_overlay_object

from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier, TrustLevel


def test_capture_writes_annotated_tag_pointing_at_structured_tree(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = tmp_path / "overlay-source"
    source_root.mkdir()

    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "agents.toml", 'name = "atlas"\n')
    _write_file(source_root / "pipelines" / "ci.yml", "name: ci\n")
    _write_file(source_root / "prompts" / "review.json", '{\n  "name": "review"\n}\n')
    _write_file(source_root / "README.md", "generic markdown should stay out of Tier A\n")
    _write_file(source_root / "ignored.py", "print('not tier a')\n")

    ref = OverlayRef(author="atlas", name="theme-dark")
    meta = OverlayMeta(
        ref=ref,
        tier=OverlayTier.A,
        trust=TrustLevel.TRUSTED,
        author="atlas",
        signature="unsigned",
        timestamp="2026-05-01T00:00:00Z",
        parent_overlay_refs=[
            "refs/overlays/team/shared-base",
            "refs/overlays/atlas/personal-tweaks",
        ],
    )

    capture_overlay_object(
        overlay_store=overlay_store,
        source_root=source_root,
        metadata=meta,
    )

    tag_oid = _git_output(overlay_store, "rev-parse", ref.ref_path)
    assert _git_output(overlay_store, "cat-file", "-t", tag_oid) == "tag"

    tag_body = _git_output(overlay_store, "cat-file", "-p", tag_oid)
    assert "type tree" in tag_body

    structured_tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")
    assert _ls_tree_names(overlay_store, structured_tree_oid) == [
        "metadata_blob",
        "staged_index_tree",
        "untracked_blobs",
        "working_tree_tree",
    ]

    metadata_blob = _git_show(overlay_store, f"{structured_tree_oid}:metadata_blob")
    assert 'author = "atlas"' in metadata_blob
    assert 'signature = "unsigned"' in metadata_blob
    assert 'timestamp = "2026-05-01T00:00:00Z"' in metadata_blob
    assert "refs/overlays/team/shared-base" in metadata_blob
    assert "refs/overlays/atlas/personal-tweaks" in metadata_blob

    working_tree_oid = _tree_entry_oid(overlay_store, structured_tree_oid, "working_tree_tree")
    staged_tree_oid = _tree_entry_oid(overlay_store, structured_tree_oid, "staged_index_tree")
    untracked_tree_oid = _tree_entry_oid(overlay_store, structured_tree_oid, "untracked_blobs")
    expected_files = [
        "COMPOSE.md",
        "agents.toml",
        "pipelines/ci.yml",
        "prompts/review.json",
    ]
    assert _flatten_tree(overlay_store, working_tree_oid) == expected_files
    assert _flatten_tree(overlay_store, staged_tree_oid) == expected_files
    assert "ignored.py" not in _flatten_tree(overlay_store, working_tree_oid)
    assert "README.md" not in _flatten_tree(overlay_store, working_tree_oid)
    assert "ignored.py" not in _flatten_tree(overlay_store, staged_tree_oid)
    assert "README.md" not in _flatten_tree(overlay_store, staged_tree_oid)
    assert _flatten_tree(overlay_store, untracked_tree_oid) == []


def test_apply_round_trips_tier_a_overlay_and_is_idempotent(tmp_path: Path) -> None:
    overlay_store = _init_bare_git_repo(tmp_path / "overlay-store.git")
    source_root = tmp_path / "overlay-source"
    target_root = tmp_path / "clean-checkout"
    source_root.mkdir()
    target_root.mkdir()

    _write_file(source_root / "COMPOSE.md", "overlay compose\n")
    _write_file(source_root / "settings.toml", 'theme = "owl"\n')
    _write_file(source_root / "skills" / "ci.yml", "steps:\n  - lint\n")
    _write_file(source_root / "prompts" / "review.json", '{\n  "prompt": "be precise"\n}\n')
    _write_file(source_root / "README.md", "generic markdown should stay out of Tier A\n")
    _write_file(source_root / "ignored.rs", "fn main() {}\n")
    _write_file(target_root / "notes.txt", "keep me\n")

    ref = OverlayRef(author="atlas", name="review-defaults")
    meta = OverlayMeta(
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
        metadata=meta,
    )

    apply_overlay_object(
        overlay_store=overlay_store,
        overlay_ref=ref,
        checkout_root=target_root,
    )
    first_snapshot = _snapshot_files(target_root)
    assert first_snapshot == {
        "COMPOSE.md": "overlay compose\n",
        "notes.txt": "keep me\n",
        "prompts/review.json": '{\n  "prompt": "be precise"\n}\n',
        "settings.toml": 'theme = "owl"\n',
        "skills/ci.yml": "steps:\n  - lint\n",
    }
    assert not (target_root / "ignored.rs").exists()
    assert not (target_root / "README.md").exists()

    apply_overlay_object(
        overlay_store=overlay_store,
        overlay_ref=ref,
        checkout_root=target_root,
    )
    second_snapshot = _snapshot_files(target_root)
    assert second_snapshot == first_snapshot


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
    entries = _git_output(git_dir, "ls-tree", "--name-only", tree_oid).splitlines()
    return sorted(entries)


def _tree_entry_oid(git_dir: Path, tree_oid: str, entry_name: str) -> str:
    line = _git_output(git_dir, "ls-tree", tree_oid, entry_name)
    return line.split()[2]


def _flatten_tree(git_dir: Path, tree_oid: str) -> list[str]:
    files = _git_output(git_dir, "ls-tree", "-r", "--name-only", tree_oid).splitlines()
    return sorted(files)


def _snapshot_files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _git_show(git_dir: Path, object_spec: str) -> str:
    return _git_output(git_dir, "show", object_spec)


def _git_output(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()

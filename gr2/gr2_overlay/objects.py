"""Overlay object encoding: capture source files into git objects, apply them back."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.types import OverlayMeta, OverlayRef

TIER_A_EXTENSIONS = frozenset({".toml", ".yml", ".json"})
TIER_A_FILENAMES = frozenset({"COMPOSE.md"})


def capture_overlay_object(
    overlay_store: Path,
    source_root: Path,
    metadata: OverlayMeta,
) -> None:
    tier_a_files = _collect_tier_a_files(source_root)

    blobs: dict[str, str] = {}
    for rel_path, abs_path in tier_a_files:
        blobs[rel_path] = _hash_blob_from_file(overlay_store, abs_path)

    working_tree_oid = _build_nested_tree(overlay_store, blobs)
    empty_tree_oid = _build_nested_tree(overlay_store, {})
    meta_blob_oid = _hash_blob_from_bytes(overlay_store, _serialize_metadata(metadata))

    structured_input = "\n".join(
        [
            f"100644 blob {meta_blob_oid}\tmetadata_blob",
            f"040000 tree {working_tree_oid}\tstaged_index_tree",
            f"040000 tree {empty_tree_oid}\tuntracked_blobs",
            f"040000 tree {working_tree_oid}\tworking_tree_tree",
        ]
    )
    structured_tree_oid = _git_input(overlay_store, structured_input, "mktree")

    tag_content = (
        f"object {structured_tree_oid}\n"
        f"type tree\n"
        f"tag {metadata.ref.name}\n"
        f"tagger overlay-system <overlay@gr2> 0 +0000\n"
        f"\n"
        f"overlay: {metadata.ref.ref_path}\n"
    )
    tag_oid = _git_input(overlay_store, tag_content, "mktag")
    _git_run(overlay_store, "update-ref", metadata.ref.ref_path, tag_oid)


def apply_overlay_object(
    overlay_store: Path,
    overlay_ref: OverlayRef,
    checkout_root: Path,
) -> None:
    tag_oid = _git_output(overlay_store, "rev-parse", overlay_ref.ref_path)
    structured_tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")

    wt_line = _git_output(overlay_store, "ls-tree", structured_tree_oid, "working_tree_tree")
    working_tree_oid = wt_line.split()[2]

    ls_output = _git_output(overlay_store, "ls-tree", "-r", working_tree_oid)
    if not ls_output:
        return

    for line in ls_output.splitlines():
        meta_part, file_path = line.split("\t", 1)
        blob_oid = meta_part.split()[2]

        content = subprocess.run(
            ["git", f"--git-dir={overlay_store}", "cat-file", "blob", blob_oid],
            check=True,
            capture_output=True,
        ).stdout

        target = checkout_root / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _collect_tier_a_files(source_root: Path) -> list[tuple[str, Path]]:
    result = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in TIER_A_EXTENSIONS or path.name in TIER_A_FILENAMES:
            rel = str(path.relative_to(source_root)).replace("\\", "/")
            result.append((rel, path))
    return result


def _build_nested_tree(overlay_store: Path, blobs: dict[str, str]) -> str:
    direct: list[tuple[str, str, str, str]] = []
    subdirs: dict[str, dict[str, str]] = {}

    for path, blob_oid in blobs.items():
        parts = path.split("/", 1)
        if len(parts) == 1:
            direct.append((parts[0], "100644", "blob", blob_oid))
        else:
            subdirs.setdefault(parts[0], {})[parts[1]] = blob_oid

    for dirname, sub_blobs in sorted(subdirs.items()):
        sub_oid = _build_nested_tree(overlay_store, sub_blobs)
        direct.append((dirname, "040000", "tree", sub_oid))

    direct.sort(key=lambda e: e[0])
    tree_input = "\n".join(f"{mode} {kind} {oid}\t{name}" for name, mode, kind, oid in direct)
    return _git_input(overlay_store, tree_input, "mktree")


def _hash_blob_from_file(overlay_store: Path, file_path: Path) -> str:
    return _git_output(overlay_store, "hash-object", "-w", str(file_path))


def _hash_blob_from_bytes(overlay_store: Path, data: bytes) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={overlay_store}", "hash-object", "-w", "--stdin"],
        input=data,
        check=True,
        capture_output=True,
    )
    return result.stdout.strip().decode()


def _serialize_metadata(meta: OverlayMeta) -> bytes:
    lines = [
        "[overlay]",
        f'author = "{meta.author}"',
        f'signature = "{meta.signature}"',
        f'timestamp = "{meta.timestamp}"',
        f'tier = "{meta.tier}"',
        f'trust = "{meta.trust}"',
        "",
        "[overlay.ref]",
        f'author = "{meta.ref.author}"',
        f'name = "{meta.ref.name}"',
    ]
    if meta.parent_overlay_refs:
        lines.append("")
        lines.append("[overlay.parents]")
        lines.append("refs = [")
        for ref in meta.parent_overlay_refs:
            lines.append(f'    "{ref}",')
        lines.append("]")
    lines.append("")
    return "\n".join(lines).encode()


def _git_output(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_input(git_dir: Path, stdin_data: str, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        input=stdin_data,
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

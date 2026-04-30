"""Overlay object encoding: capture source files into git objects, apply them back."""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2_overlay.types import OverlayMeta, OverlayRef, OverlayTier

TIER_A_EXTENSIONS = frozenset({".toml", ".yml", ".json"})
TIER_A_FILENAMES = frozenset({"COMPOSE.md"})


def capture_overlay_object(
    overlay_store: Path,
    source_root: Path,
    metadata: OverlayMeta,
) -> None:
    if metadata.tier == OverlayTier.B:
        _capture_tier_b(overlay_store, source_root, metadata)
    else:
        _capture_tier_a(overlay_store, source_root, metadata)


def _capture_tier_a(
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

    _finalize_overlay_tag(
        overlay_store, metadata, meta_blob_oid, working_tree_oid, working_tree_oid, empty_tree_oid
    )


def _capture_tier_b(
    overlay_store: Path,
    source_root: Path,
    metadata: OverlayMeta,
) -> None:
    dirty_files = _get_dirty_tracked_files(source_root)

    staged_blobs: dict[str, str] = {}
    working_blobs: dict[str, str] = {}
    deleted_paths: list[str] = []

    for rel_path in dirty_files:
        index_oid = _get_index_blob_oid(source_root, rel_path)
        abs_path = source_root / rel_path

        if index_oid is None and not abs_path.exists():
            deleted_paths.append(rel_path)
            continue

        if index_oid is not None:
            index_content = _read_blob_from_repo(source_root, index_oid)
            staged_blobs[rel_path] = _hash_blob_from_bytes(overlay_store, index_content)

        if abs_path.exists():
            working_blobs[rel_path] = _hash_blob_from_file(overlay_store, abs_path)

    staged_tree_oid = _build_nested_tree(overlay_store, staged_blobs)
    working_tree_oid = _build_nested_tree(overlay_store, working_blobs)
    empty_tree_oid = _build_nested_tree(overlay_store, {})

    repo_state = _get_repo_state(source_root)
    meta_blob_oid = _hash_blob_from_bytes(
        overlay_store,
        _serialize_metadata(metadata, repo_state=repo_state, deleted_paths=deleted_paths),
    )

    _finalize_overlay_tag(
        overlay_store, metadata, meta_blob_oid, staged_tree_oid, working_tree_oid, empty_tree_oid
    )


def apply_overlay_object(
    overlay_store: Path,
    overlay_ref: OverlayRef,
    checkout_root: Path,
) -> None:
    tag_oid = _git_output(overlay_store, "rev-parse", overlay_ref.ref_path)
    structured_tree_oid = _git_output(overlay_store, "rev-parse", f"{tag_oid}^{{tree}}")

    meta_blob = _read_metadata_blob(overlay_store, structured_tree_oid)
    is_tier_b = 'tier = "source"' in meta_blob

    _write_tree_to_disk(overlay_store, structured_tree_oid, "working_tree_tree", checkout_root)

    if is_tier_b:
        _apply_staged_index(overlay_store, structured_tree_oid, checkout_root)
        _apply_deletions(meta_blob, checkout_root)


def _finalize_overlay_tag(
    overlay_store: Path,
    metadata: OverlayMeta,
    meta_blob_oid: str,
    staged_tree_oid: str,
    working_tree_oid: str,
    untracked_tree_oid: str,
) -> None:
    structured_input = "\n".join(
        [
            f"100644 blob {meta_blob_oid}\tmetadata_blob",
            f"040000 tree {staged_tree_oid}\tstaged_index_tree",
            f"040000 tree {untracked_tree_oid}\tuntracked_blobs",
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


def _get_dirty_tracked_files(source_root: Path) -> list[str]:
    staged = _git_lines_in_repo(source_root, "diff-index", "--cached", "--name-only", "HEAD")
    unstaged = _git_lines_in_repo(source_root, "diff-files", "--name-only")
    return sorted(set(staged) | set(unstaged))


def _get_index_blob_oid(source_root: Path, rel_path: str) -> str | None:
    output = _git_output_in_repo(source_root, "ls-files", "-s", rel_path)
    if not output:
        return None
    return output.split()[1]


def _read_blob_from_repo(repo_root: Path, blob_oid: str) -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", blob_oid],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _get_repo_state(source_root: Path) -> dict[str, object]:
    head_commit = _git_output_in_repo(source_root, "rev-parse", "HEAD")
    try:
        head_ref = _git_output_in_repo(source_root, "symbolic-ref", "HEAD")
        head_branch = head_ref.removeprefix("refs/heads/")
        head_detached = False
    except subprocess.CalledProcessError:
        head_ref = ""
        head_branch = ""
        head_detached = True
    return {
        "repo": ".",
        "head_branch": head_branch,
        "head_ref": head_ref,
        "head_detached": head_detached,
        "head_commit": head_commit,
    }


def _read_metadata_blob(overlay_store: Path, structured_tree_oid: str) -> str:
    return _git_output(overlay_store, "show", f"{structured_tree_oid}:metadata_blob")


def _apply_deletions(meta_blob: str, checkout_root: Path) -> None:
    deleted_paths = _parse_deleted_paths(meta_blob)
    for rel_path in deleted_paths:
        target = checkout_root / rel_path
        if target.exists():
            target.unlink()
            parent = target.parent
            if parent != checkout_root and not any(parent.iterdir()):
                parent.rmdir()
        subprocess.run(
            ["git", "update-index", "--force-remove", rel_path],
            cwd=checkout_root,
            check=True,
            capture_output=True,
            text=True,
        )


def _parse_deleted_paths(meta_blob: str) -> list[str]:
    import tomllib

    try:
        data = tomllib.loads(meta_blob)
    except tomllib.TOMLDecodeError:
        return []
    return data.get("deleted_paths", [])


def _write_tree_to_disk(
    overlay_store: Path,
    structured_tree_oid: str,
    tree_name: str,
    target_root: Path,
) -> None:
    tree_line = _git_output(overlay_store, "ls-tree", structured_tree_oid, tree_name)
    tree_oid = tree_line.split()[2]

    ls_output = _git_output(overlay_store, "ls-tree", "-r", tree_oid)
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

        target = target_root / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _apply_staged_index(
    overlay_store: Path,
    structured_tree_oid: str,
    checkout_root: Path,
) -> None:
    staged_line = _git_output(overlay_store, "ls-tree", structured_tree_oid, "staged_index_tree")
    staged_tree_oid = staged_line.split()[2]

    ls_output = _git_output(overlay_store, "ls-tree", "-r", staged_tree_oid)
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

        result = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=checkout_root,
            input=content,
            check=True,
            capture_output=True,
        )
        local_oid = result.stdout.strip().decode()

        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{local_oid},{file_path}"],
            cwd=checkout_root,
            check=True,
            capture_output=True,
            text=True,
        )


def _git_output_in_repo(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_lines_in_repo(repo_root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


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


def _serialize_metadata(
    meta: OverlayMeta,
    *,
    repo_state: dict[str, object] | None = None,
    deleted_paths: list[str] | None = None,
) -> bytes:
    lines = [
        f'author = "{meta.author}"',
        f'signature = "{meta.signature}"',
        f'tier = "{meta.tier}"',
        f'timestamp = "{meta.timestamp}"',
        f'trust = "{meta.trust}"',
    ]
    if deleted_paths:
        items = ", ".join(f'"{p}"' for p in deleted_paths)
        lines.append(f"deleted_paths = [{items}]")
    elif deleted_paths is not None:
        lines.append("deleted_paths = []")
    lines.append("")
    lines.append("[ref]")
    lines.append(f'author = "{meta.ref.author}"')
    lines.append(f'name = "{meta.ref.name}"')
    if meta.parent_overlay_refs:
        lines.append("")
        lines.append("[parents]")
        lines.append("refs = [")
        for ref in meta.parent_overlay_refs:
            lines.append(f'    "{ref}",')
        lines.append("]")
    if repo_state:
        lines.append("")
        lines.append("[[repo_state]]")
        for key in ("repo", "head_branch", "head_ref", "head_detached", "head_commit"):
            value = repo_state.get(key)
            if isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            else:
                lines.append(f'{key} = "{value}"')
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

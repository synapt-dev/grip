"""`gr2 footprint`: part of the state gr2 writes for one workspace (slice 1, not yet complete), read-only.

Every row takes its path from the seam its writer uses (a constant or helper), never from a literal of its own, so a
writer that moves takes the listing with it. The verb writes nothing, takes no lock and reads refs with
`for-each-ref` only.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitops, gr_shadow, grip, grip_cli, layout, review_ephemeral, store_hooks

REMOVE, KEEP_USER, KEEP_SHARED, NOT_GR2 = "REMOVE", "KEEP-USER", "KEEP-SHARED", "NOT-GR2"
DISPOSITIONS = (REMOVE, KEEP_USER, KEEP_SHARED, NOT_GR2)


@dataclass
class Row:
    kind: str
    disposition: str
    written_by: str
    seam: str
    path: str | None = None
    ref: str | None = None
    store: str | None = None
    size: int | None = None
    line: str | None = None
    remove_with: str | None = None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return gitops.run(cwd, "--no-optional-locks", *args)


def _size(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    total = 0
    for base, dirs, files in os.walk(path):
        for name in dirs + files:
            total += os.lstat(os.path.join(base, name)).st_size
    return total


def _kind(path: Path) -> str:
    return "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"


def _path_row(rows: list[Row], path: Path, disposition: str, written_by: str, seam: str) -> None:
    if os.path.lexists(path):
        rows.append(Row(_kind(path), disposition, written_by, seam, path=str(path), size=_size(path)))


def _ref_rows(rows: list[Row], repo: Path, written_by: str, seam: str) -> None:
    out = _git(repo, "for-each-ref", "--format=%(refname)", grip_cli.GRIP_REF_ROOT)
    for ref in out.stdout.split("\n") if out.returncode == 0 else ():
        if ref:
            rows.append(Row("ref", REMOVE, written_by, seam, ref=ref, store=str(repo)))


def is_workspace(root: Path) -> bool:
    return (grip._is_native_workspace(root) or layout.grip_dir(root).is_dir()
            or (root / layout.MOVED_MARKER).exists())


def footprint(root: Path) -> list[Row]:
    rows: list[Row] = []
    # One row per top-level entry of `.grip/`, not one for the dir: `checkouts/` is gr1's, so each checkout is NOT-GR2,
    # named with the gr1 verb that removes it, and stays out of remove_bytes.
    grip_dir = layout.grip_dir(root)
    # A symlinked `.grip` is one row for the link itself, never followed: the store behind it gets no rows.
    if os.path.islink(grip_dir):
        _path_row(rows, grip_dir, REMOVE, "store init, review bind, lane create", "layout.grip_dir")
    for entry in sorted(grip_dir.iterdir()) if grip_dir.is_dir() and not grip_dir.is_symlink() else ():
        if entry.name == layout.GR1_CHECKOUTS_DIR and entry.is_dir() and not entry.is_symlink():
            for checkout in sorted(entry.iterdir()):
                rows.append(Row(_kind(checkout), NOT_GR2, "gr1 gr checkout add", "layout.GR1_CHECKOUTS_DIR",
                                path=str(checkout), size=_size(checkout),
                                remove_with=f"gr checkout remove {checkout.name}"))
        else:
            _path_row(rows, entry, REMOVE, "store init, review bind, lane create", "layout.grip_dir")
    _path_row(rows, root / layout.LAYOUT[0].spelling, REMOVE, "store init", "layout.LAYOUT[grip.toml]")
    if (root / ".git").exists():
        # Ask git where the exclude file is, as grip.exclude_grip_state (its only writer) does.
        found = _git(root, "rev-parse", "--git-path", "info/exclude")
        exclude = root / found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None
        if (exclude is not None and exclude.is_file()
                and grip._REVIEW_STORE_EXCLUDE in exclude.read_text(errors="replace").splitlines()):
            rows.append(Row("line", REMOVE, "store init", "grip.exclude_grip_state", path=str(exclude),
                            line=grip._REVIEW_STORE_EXCLUDE))
        hooks = store_hooks._hooks_dir(root)
        for name in store_hooks.HOOK_NAMES:
            if store_hooks._is_ours(hooks / name):
                _path_row(rows, hooks / name, REMOVE, "store init, store materialize", "store_hooks.MARKER")
        _ref_rows(rows, root, "store init, store commit, review bind, check run", "grip_cli.GRIP_REF_ROOT")
    _path_row(rows, root / grip_cli.SLOT_FILE, REMOVE, "any verb with GR2_SLOT set", "grip_cli.SLOT_FILE")
    _path_row(rows, root / layout.MOVED_MARKER, REMOVE, "store move", "layout.MOVED_MARKER")
    try:
        members = grip_cli._native_members(root) if grip._is_native_workspace(root) else []
    except grip_cli.NativeStoreRefusal:
        members = []
    for member in members:
        path = root / member.get("path", member.get("name", ""))
        if path != root and (path / ".git").exists():
            _path_row(rows, path, KEEP_USER, "store add, clone", "grip_cli._native_members")
            _ref_rows(rows, path, "check run", "grip_cli.GRIP_REF_ROOT")
    review = app_review_root(root)
    _path_row(rows, review, REMOVE, "review open", "app._default_review_lane_dir")
    # The shim keys its state by the install prefix it runs under, not the workspace: shared by every workspace.
    prefix = sys.prefix
    _path_row(rows, Path(prefix) / gr_shadow.MARKER, KEEP_SHARED, "gr shim", "gr_shadow.MARKER")
    _path_row(rows, gr_shadow._user_state(prefix), KEEP_SHARED, "gr shim", "gr_shadow._user_state")
    # Shared by every workspace and can be many GB: listed, not walked.
    cache = review_ephemeral.review_cache_root()
    if os.path.lexists(cache):
        rows.append(Row(_kind(cache), KEEP_SHARED, "review open", "review_ephemeral.review_cache_root", path=str(cache)))
    return rows


def app_review_root(root: Path) -> Path:
    # The lane dir helper refuses an existing lane, so ask it for a lane under a sha that cannot be one and take its
    # parent: `<workspace>.review`, the helper's own spelling.
    from .app import _default_review_lane_dir
    return _default_review_lane_dir(root, "g" * 8).parent


def as_json(root: Path, rows: list[Row]) -> dict:
    totals = {d: sum(1 for r in rows if r.disposition == d) for d in DISPOSITIONS}
    totals["remove_bytes"] = sum(r.size or 0 for r in rows if r.disposition == REMOVE)
    return {"kind": "gr2-footprint", "workspace": str(root), "complete": False, "rows": [asdict(r) for r in rows], "totals": totals}


def table(rows: list[Row]) -> str:
    lines = [f"{'DISPOSITION':<12} {'KIND':<7} {'SIZE':>9}  {'WRITTEN BY':<40} PATH / REF"]
    for d in DISPOSITIONS:
        for r in (r for r in rows if r.disposition == d):
            where = r.ref or (f"{r.path}: {r.line}" if r.line else r.path)
            if r.remove_with:
                where += f" (remove with: {r.remove_with})"
            size = "-" if r.size is None else str(r.size)
            lines.append(f"{r.disposition:<12} {r.kind:<7} {size:>9}  {r.written_by:<40} {where}")
    lines.append("total " + "; ".join(f"{d} {sum(1 for r in rows if r.disposition == d)}" for d in DISPOSITIONS))
    lines.append("partial: not every path gr2 writes is listed yet")
    return "\n".join(lines)

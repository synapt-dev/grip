"""Which directory is the workspace: the walk every verb shares.

Kept apart from ``app`` so a verb dispatched without building the whole CLI (``review stamp``
through the dispatch table) can infer its root without importing ``app``. ``app`` re-exports
both names, so callers and tests that reach them through ``app`` are unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import grip, layout


def _is_workspace_root(path: Path) -> bool:
    """A directory that IS a workspace, of any kind: it holds ``.grip/workspace_spec.toml``, it is a
    native store root (``grip.toml`` beside a root ``.git``, what ``store init`` makes), or it is a
    full-mode root, which has neither file: a git root with a stook, or whose HEAD holds a
    ``<m>.griplink/001.2_pin`` gitlink (the shape ``root_verbs.find`` reads, so both walks agree)."""
    if (layout.grip_dir(path) / "workspace_spec.toml").is_file() or grip._is_native_workspace(path):
        return True
    from . import root_verbs

    return root_verbs._is_full_mode_shape(path)


def _resolve_workspace_root(workspace_root: Optional[Path] = None) -> Path:
    """The workspace root for a verb: the argument if given, else the nearest
    ancestor of the current directory (the directory itself included) that is a
    workspace of EITHER kind, else the current directory.

    NEAREST, across both kinds. A store root has no ``workspace_spec.toml``, so walking
    for the spec file alone let any outer initialised workspace win over the store root
    the caller was standing in: ``review bind`` then exited 0 having bound in the outer
    workspace, and every read of that id against the caller's own root said it bound nothing.

    Every verb took this as a REQUIRED bare positional until now, so a stranger
    running ``gr2 spec validate`` from inside their own workspace got
    "Missing argument 'workspace_root'" and read the tool as broken. The
    explicit form is unchanged: a value passed in wins, and is still resolved.
    """
    if workspace_root is not None:
        return workspace_root.resolve()
    cwd = Path.cwd().resolve()
    return next((path for path in (cwd, *cwd.parents) if _is_workspace_root(path)), cwd)

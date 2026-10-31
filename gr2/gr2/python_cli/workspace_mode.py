"""The workspace mode in grip.toml: "compat" (the default) or "full".

A full-mode grip tree keeps a mode-160000 entry per member, but the entry points at a FIELD-COMMIT, not at the member's
commit, so a reader that takes a tree entry for the member's pin cannot tell the two apart by the tree alone. Every such
reader asks this module first and either handles full mode or refuses it by name. (A member table's own
`mode = "full"` is a different key: how the member is checked out.)
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Mapping

from . import gitops

MODES = ("compat", "full")


class WorkspaceModeError(ValueError):
    """A grip.toml whose top-level `mode` is neither "compat" nor "full"."""


def of_document(document: Mapping) -> str:
    mode = document.get("mode", "compat")
    if mode not in MODES:
        raise WorkspaceModeError(f'grip.toml mode must be "compat" or "full", got {mode!r}')
    return mode


def of_root(root: Path, revision: str | None = None) -> str:
    """The mode of the workspace at ``root``: its working grip.toml (or, with a ``revision``, the one committed there).
    A root with no grip.toml is full when it has the shape `store init --mode full` makes, else compat."""
    root = Path(root)
    try:
        if revision is None:
            text = (root / "grip.toml").read_text(encoding="utf-8")
        else:
            shown = gitops.run(root, "show", f"{revision}:grip.toml")
            if shown.returncode:
                return _no_document(root)
            text = shown.stdout
        return of_document(tomllib.loads(text))
    except FileNotFoundError:
        return _no_document(root)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "compat"


def _no_document(root: Path) -> str:
    """No grip.toml to read: full when the root is a full-mode root (workspace_guidance.is_full_mode_root, which reads
    the root's current shape, not ``revision``'s), else compat."""
    from .workspace_guidance import is_full_mode_root

    return "full" if is_full_mode_root(root) else "compat"


def of_grip_toml(root: Path, revision: str | None = None) -> str:
    """The mode grip.toml declares, and only that: "full" only where grip.toml says mode = "full". A full-mode
    root's `<m>.griplink/001.2_pin` is the member's real commit, so a reader that takes a tree entry as the pin asks
    this, not of_root."""
    root = Path(root)
    try:
        if revision is None:
            text = (root / "grip.toml").read_text(encoding="utf-8")
        else:
            shown = gitops.run(root, "show", f"{revision}:grip.toml")
            if shown.returncode:
                return "compat"
            text = shown.stdout
        return of_document(tomllib.loads(text))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "compat"


def full_mode_refusal(reader: str, root: Path) -> str:
    """The named sentence a compat-only reader refuses a full-mode workspace with, ending in the verb that reads it."""
    from .next_steps import _shown

    return (f'full_mode_unsupported: {reader} of {_shown(str(root))} does not read a full-mode workspace (this '
            f'workspace\'s grip.toml says mode = "full"): run gr2 status in {_shown(str(root))} to read it')

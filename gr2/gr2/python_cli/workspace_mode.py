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
    A root with no readable document is compat, which is what every store written before the key existed is."""
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


def full_mode_refusal(reader: str) -> str:
    """The named sentence a compat-only reader refuses a full-mode workspace with."""
    return (f'full_mode_unsupported: {reader} reads a tree entry as the member\'s pin, but this workspace\'s grip.toml '
            'says mode = "full", where the entry is a field-commit; it cannot judge this workspace')

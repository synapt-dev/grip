"""The record a root wrote before the field sidecar: the member's gitlink names a FIELD-COMMIT whose tree holds the
record. A revision with no `.grip/fields` is read this way; nothing writes it any more."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import member_field_tree as mft


class LegacyRecordError(Exception):
    """The field-commit cannot be read; the message says why, and ``__cause__`` is the reader's own error."""


def read(root: Path, field_commit: str) -> dict:
    """The record the field-commit ``field_commit`` carries in its tree."""
    try:
        return mft.read(root, f"{field_commit}^{{tree}}")
    except (mft.FieldTreeError, RuntimeError, subprocess.SubprocessError, OSError) as exc:
        # the reader's own error stays the cause: the caller reads the unshallow hint from it
        raise LegacyRecordError(str(exc)) from exc

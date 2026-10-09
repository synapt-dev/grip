"""One line, once per install, when another `gr` earlier on PATH would answer instead of this package's `gr`.

This package installs `gr` beside `gr2`. A gr1 release before 1.6 also installs a `gr`, and it has no resolver: when
it comes first on PATH, `gr` runs gr1 everywhere, outside any workspace too, and the user never reaches gr2 by `gr`.
`gr2` is the name that always reaches this package, so it is the one that can say so.

The line prints at most once per install: the judged `gr` is recorded in the install's own prefix, and a `gr` that was
already judged is not judged again. A prefix that cannot be written prints nothing, because a line that could not be
recorded would print on every run (the record is written before `gr --version` is read for the same reason: an
unwritable prefix would otherwise run that check on every command). It never prints under `--json`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = ".gr2-gr-shadow"
RESOLVER_FROM = (1, 6)  # gitgrip 1.6 ships the resolver, so its `gr` applies the same table as ours


def _gr1_version(gr: str) -> tuple[int, int, str] | None:
    """(major, minor, text) when `gr --version` reads `gr X.Y.Z`; None for anything else."""
    env = {k: v for k, v in os.environ.items() if k != "GR_RESOLVED"}
    try:
        out = subprocess.run([gr, "--version"], capture_output=True, text=True, timeout=2, env=env).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"gr (\d+)\.(\d+)\.\S+", out.strip())
    return (int(m.group(1)), int(m.group(2)), m.group(0)[3:]) if m else None


def shadow_line(argv: list[str], argv0: str, prefix: str) -> str | None:
    """The line to print, recording that it was shown; None when there is nothing to say or it was said before."""
    if "--json" in argv:
        return None
    ours = shutil.which("gr", path=str(Path(argv0).resolve().parent))
    first = shutil.which("gr")
    if not ours or not first or os.path.realpath(first) == os.path.realpath(ours):
        return None
    judged = os.path.realpath(first)
    marker = Path(prefix) / MARKER
    try:
        if marker.read_text() == judged:
            return None
    except OSError:
        pass
    try:
        marker.write_text(judged)
    except OSError:
        return None  # unrecorded, it would print on every run
    version = _gr1_version(first)
    if version is None or version[:2] >= RESOLVER_FROM:
        return None
    return (f"gr2: the `gr` first on PATH is gitgrip {version[2]} ({first}), so `gr` runs gr1 everywhere. "
            f"Type `gr2`, or put {Path(ours).parent} before {Path(first).parent} on PATH. (shown once)")


def note_shadow() -> None:
    line = shadow_line(sys.argv[1:], sys.argv[0], sys.prefix)
    if line:
        print(line, file=sys.stderr)

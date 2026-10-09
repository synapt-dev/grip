"""One line, once per install, when another `gr` earlier on PATH would answer instead of this package's `gr`.

This package installs `gr` beside `gr2`. A gr1 release before 1.6 also installs a `gr`, and it has no resolver: when
it comes first on PATH, `gr` runs gr1 everywhere, outside any workspace too, and the user never reaches gr2 by `gr`.
`gr2` is the name that always reaches this package, so it is the one that can say so.

The line prints at most once per install. What was judged (the first `gr`'s path and modification time) is recorded
in the install's own prefix, or, when the prefix cannot be written (a `pip install --user` into a system Python), in a
per-user state file keyed by the prefix. Only a definite answer is recorded: a `gr --version` that times out is judged
again next time, so one slow first run does not silence the line for good. A file replaced in place is judged again,
because its modification time changes. It never prints under `--json` or `GR2_QUIET_CONTEXT`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = ".gr2-gr-shadow"
RESOLVER_FROM = (1, 6)  # gitgrip 1.6 ships the resolver, so its `gr` applies the same table as ours
_TIMEOUT = object()


def _gr1_version(gr: str):
    """(major, minor, text) when `gr --version` reads `gr X.Y.Z`; None for any other answer; _TIMEOUT for none."""
    env = {k: v for k, v in os.environ.items() if k != "GR_RESOLVED"}
    try:
        out = subprocess.run([gr, "--version"], capture_output=True, text=True, timeout=2, env=env).stdout
    except subprocess.TimeoutExpired:
        return _TIMEOUT
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"gr (\d+)\.(\d+)\.\S+", out.strip())
    return (int(m.group(1)), int(m.group(2)), m.group(0)[3:]) if m else None


def _user_state(prefix: str) -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    key = hashlib.sha256(os.path.realpath(prefix).encode()).hexdigest()[:16]
    return Path(base) / "gr2" / f"gr-shadow-{key}"


def _record(prefix: str, value: str) -> bool:
    """Write the judged value to the prefix, else to the per-user state file; False when neither can be written."""
    for target in (Path(prefix) / MARKER, _user_state(prefix)):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
            return True
        except OSError:
            continue
    return False


def _recorded(prefix: str) -> set[str]:
    seen = set()
    for target in (Path(prefix) / MARKER, _user_state(prefix)):
        try:
            seen.add(target.read_text())
        except OSError:
            pass
    return seen


def shadow_line(argv: list[str], argv0: str, prefix: str) -> str | None:
    """The line to print, recording that it was shown; None when there is nothing to say or it was said before."""
    if "--json" in argv or os.environ.get("GR2_QUIET_CONTEXT"):
        return None
    ours = shutil.which("gr", path=str(Path(argv0).resolve().parent))
    first = shutil.which("gr")
    if not ours or not first or os.path.realpath(first) == os.path.realpath(ours):
        return None
    real = os.path.realpath(first)
    try:
        judged = f"{real} {os.stat(real).st_mtime_ns}"
    except OSError:
        return None
    if judged in _recorded(prefix):
        return None
    version = _gr1_version(first)
    if version is _TIMEOUT:
        return None  # not a definite answer: judge it again next time
    if not _record(prefix, judged):
        return None  # unrecorded, it would print on every run
    if version is None or version[:2] >= RESOLVER_FROM:
        return None
    return (f"gr2: the `gr` first on PATH is gitgrip {version[2]} ({first}), so `gr` runs gr1 everywhere. "
            f"Type `gr2`, or put {Path(ours).parent} before {Path(first).parent} on PATH. (shown once)")


def note_shadow() -> None:
    line = shadow_line(sys.argv[1:], sys.argv[0], sys.prefix)
    if line:
        print(line, file=sys.stderr)

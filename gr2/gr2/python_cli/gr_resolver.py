"""The `gr` command, Python half: pick gr1 or gr2 by the nearest workspace marker and run it.

This module is not yet wired to a `gr` command: a console script named `gr` would shadow an existing gr1 on a
user's PATH, so the entry point ships with the Rust half. Run it with `python -m gr2.python_cli.gr_resolver`.

The rule is a table (`conformance/gr-resolver/cases.toml`), and this file only applies it:

* the nearest marker wins: `.gitgrip` means gr1; `grip.toml` beside a `.git`, or a workspace spec under the
  grip directory, means gr2; no marker means gr2;
* when one directory holds both, gr1 wins and the one stderr line says the directory also holds a gr2 workspace;
* the chosen half is run from PATH with `GR_RESOLVED` set, and a second resolution refuses (exit 70);
* a missing half refuses in one sentence (exit 69);
* `GR2_QUIET_CONTEXT` silences only the informational line, never a refusal;
* invoked under the name of a half (`gr1` or `gr2`), it runs that half and resolves nothing;
* `--which` names a second `gr` on PATH that is not this one, because a stale install answers instead of this one.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .layout import grip_dir

GR1_MARKER = ".gitgrip"
GR2_ROOT_FILE = "grip.toml"
SPEC_FILE = "workspace_spec.toml"
EXIT_LOOP = 70
EXIT_MISSING_HALF = 69


def _is_gr2(d: Path) -> bool:
    return ((d / GR2_ROOT_FILE).is_file() and (d / ".git").exists()) or (grip_dir(d) / SPEC_FILE).is_file()


def _is_gr1(d: Path) -> bool:
    return (d / GR1_MARKER).exists()


def resolve(cwd: Path) -> tuple[str, str, str]:
    """Return (kind, deciding marker path or '', note) for the nearest marker above `cwd`."""
    d = cwd
    while True:
        if _is_gr1(d):
            note = "; this root also holds a gr2 workspace, use gr2 for it" if _is_gr2(d) else ""
            return "gr1", str(d / GR1_MARKER), note
        if _is_gr2(d):
            return "gr2", str(d), ""
        if d == d.parent:
            return "gr2", "", ""
        d = d.parent


def _other_grs_on_path(own: str) -> list[str]:
    """Every executable named `gr` on PATH that is not this command."""
    seen, found = set(), []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry or ".") / "gr"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            real = os.path.realpath(candidate)
            if real != own and real not in seen:
                seen.add(real)
                found.append(str(candidate))
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    invoked_as = Path(sys.argv[0]).name
    if invoked_as in ("gr1", "gr2"):  # explicit: run the named half, resolve nothing
        found_own = shutil.which(invoked_as)
        if not found_own:
            print(f"gr: this needs {invoked_as}, which is not installed", file=sys.stderr)
            return EXIT_MISSING_HALF
        os.execve(found_own, [invoked_as, *args], dict(os.environ))
    resolved = os.environ.get("GR_RESOLVED")
    if resolved:
        print(f"gr: refusing to resolve twice (GR_RESOLVED={resolved})", file=sys.stderr)
        return EXIT_LOOP
    cwd = Path(os.path.realpath(os.getcwd()))
    kind, where, note = resolve(cwd)
    found = shutil.which(kind)
    if args[:1] == ["--which"]:
        others = _other_grs_on_path(os.path.realpath(sys.argv[0]))
        if others:
            print(f"gr: another `gr` is on PATH: {others[0]}; it may answer instead of this one", file=sys.stderr)
        print(f"{kind} {where or 'none'} {found or 'missing'}")
        return 0
    quiet = bool(os.environ.get("GR2_QUIET_CONTEXT"))
    if not quiet and (note or (where and str(Path(where).parent) != str(cwd) and where != str(cwd))):
        print(f"gr: {kind} ({where or 'no workspace marker'}{note})", file=sys.stderr)
    if not found:
        print(f"gr: this needs {kind}, which is not installed", file=sys.stderr)
        return EXIT_MISSING_HALF
    os.execve(found, [kind, *args], {**os.environ, "GR_RESOLVED": kind})
    return 0  # unreachable: execve replaces the process


if __name__ == "__main__":
    sys.exit(main())

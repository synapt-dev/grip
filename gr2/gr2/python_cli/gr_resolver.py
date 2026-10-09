"""The `gr` command, Python half: pick gr1 or gr2 by the nearest workspace marker and run it.

The PyPI package installs it as `gr`. gr1 is found on PATH as `gr1`, else as `gitgrip`: every gr1 release
installs `gr` and `gitgrip`, and under the name `gitgrip` gr1 runs itself without resolving.

The rule is a table (`conformance/gr-resolver/cases.toml`), and this file only applies it:

* the nearest marker wins: `.gitgrip` means gr1; `grip.toml` beside a `.git`, or a workspace spec under the
  grip directory, means gr2; no marker means gr2;
* when one directory holds both, gr1 wins and the one stderr line says the directory also holds a gr2 workspace;
  with gr1 not installed it is a migrated workspace, so gr2 serves it and the one line says so;
* the chosen half is run from PATH with `GR_RESOLVED` set, and a second resolution refuses (exit 70);
* a missing half refuses in one sentence (exit 69); for gr1 the sentence names the two ways forward;
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
GR1_NAMES = ("gr1", "gitgrip")
GR1_MISSING = ("gr: this is a gr1 workspace (.gitgrip) and gr1 is not installed. Run `gr2 workspace migrate-gr1` "
               "once and `gr` works here, or install gr1: brew install synapt-dev/tap/gitgrip")


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


def _exec(path: str, argv: list[str], env: dict[str, str]) -> int:
    """Run the chosen half in place of this process. On Windows `os.execve` starts a new process and this one exits
    at once with 0, losing the half's exit code, so there the half runs as a child and its code is returned."""
    if os.name == "nt":
        import subprocess

        return subprocess.run([path, *argv[1:]], env=env).returncode
    os.execve(path, argv, env)
    return 0  # unreachable: execve replaces the process


class _FoundSelf(Exception):
    """The only executable under a half's name is this resolver: running it would resolve again, forever."""


def _find(kind: str) -> str | None:
    """The executable that runs `kind`: gr2 as `gr2`; gr1 as `gr1`, else as `gitgrip`. One that is this resolver
    is skipped; when it is the only one, `_FoundSelf` names it instead of a loop."""
    own = _own()
    selves = []
    for name in (GR1_NAMES if kind == "gr1" else (kind,)):
        found = shutil.which(name)
        if found and os.path.normcase(os.path.realpath(found)) == own:
            selves.append(found)
        elif found:
            return found
    if selves:
        raise _FoundSelf(selves[0])
    return None


def _own() -> str:
    """This command's real path. On Windows argv[0] can lack the `.exe` that `shutil.which` returns."""
    own = os.path.realpath(sys.argv[0])
    if os.name == "nt" and not os.path.splitext(own)[1] and os.path.isfile(own + ".exe"):
        own += ".exe"
    return os.path.normcase(own)


def _other_grs_on_path(own: str) -> list[str]:
    """Every executable named `gr` on PATH that is not this command (on Windows, `gr.exe` and the rest of PATHEXT)."""
    seen, found = set(), []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = shutil.which("gr", path=entry or ".")
        if candidate:
            real = os.path.normcase(os.path.realpath(candidate))
            if real != own and real not in seen:
                seen.add(real)
                found.append(str(candidate))
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    invoked_as = Path(sys.argv[0]).name
    if invoked_as in ("gr1", "gr2"):  # explicit: run the named half, resolve nothing
        try:
            found_own = _find(invoked_as)
        except _FoundSelf as exc:
            print(f"gr: {exc} is this resolver, not {invoked_as}; refusing to run itself", file=sys.stderr)
            return EXIT_LOOP
        if not found_own:
            print(f"gr: this needs {invoked_as}, which is not installed", file=sys.stderr)
            return EXIT_MISSING_HALF
        return _exec(found_own, [invoked_as, *args], dict(os.environ))
    resolved = os.environ.get("GR_RESOLVED")
    if resolved:
        print(f"gr: refusing to resolve twice (GR_RESOLVED={resolved})", file=sys.stderr)
        return EXIT_LOOP
    cwd = Path(os.path.realpath(os.getcwd()))
    kind, where, note = resolve(cwd)
    try:
        found = _find(kind)
        if kind == "gr1" and note and not found:
            # Both markers and no gr1: the workspace was migrated and gr1 cannot write here, so gr2 serves it.
            kind, note, found = "gr2", "; gr1 is not installed, so gr2 serves this migrated workspace", _find("gr2")
    except _FoundSelf as exc:
        print(f"gr: {exc} is this resolver, not {kind}; refusing to run itself", file=sys.stderr)
        return EXIT_LOOP
    if args[:1] == ["--which"]:
        others = _other_grs_on_path(_own())
        if others:
            print(f"gr: another `gr` is on PATH: {others[0]}; it may answer instead of this one", file=sys.stderr)
        print(f"{kind} {where or 'none'} {found or 'missing'}")
        return 0
    quiet = bool(os.environ.get("GR2_QUIET_CONTEXT"))
    if not quiet and (note or (where and str(Path(where).parent) != str(cwd) and where != str(cwd))):
        print(f"gr: {kind} ({where or 'no workspace marker'}{note})", file=sys.stderr)
    if not found:
        print(GR1_MISSING if kind == "gr1" else f"gr: this needs {kind}, which is not installed", file=sys.stderr)
        return EXIT_MISSING_HALF
    return _exec(found, [kind, *args], {**os.environ, "GR_RESOLVED": kind})


if __name__ == "__main__":
    sys.exit(main())

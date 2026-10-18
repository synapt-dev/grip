"""The help surface: what `gr2 --help` shows and what `gr2 help -a` adds.

`gr2 --help` and bare `gr2` show the porcelain: what a reader or an agent types in the walkthrough.
Plumbing (what scripts, CI and other gr2 verbs call) is registered with hidden=True and listed by
`gr2 help -a`; every plumbing path still runs by its full name. COMPAT_PATHS are hidden paths kept only so
old callers keep working: they are listed nowhere, and they still run. COMPAT_GROUPS does the same for a whole
alias group (`grip`, the alias of `store`).

tests/test_cli_help_surface.py pins the porcelain set, so a new visible command has to be placed in it on
purpose, and a new hidden one is plumbing unless it is added here.
"""
from __future__ import annotations

COMPAT_PATHS = frozenset({
    "grip snapshot",
    "lane current",
    "repo hook-run",
    "repo hooks",
    "review close-gr",
    "review exit-gr",
    "review open-gr",
    "review open-project",
    "review requirements",
    "store snapshot",
})

# The lister itself is hidden so the porcelain stays at its count; it is named in the root help instead.
_NOT_LISTED = COMPAT_PATHS | {"help"}

# A hidden alias GROUP (registered in api/deprecations.toml) is compat as a whole: every path under it is listed
# nowhere, so `gr2 help -a` never teaches a name that is due to be removed beside its twin.
COMPAT_GROUPS = frozenset({"grip"})


def leaves(group, path: str = "", hidden_above: bool = False) -> list[tuple[str, object, bool]]:
    """Every runnable command under a built click group: (path, command, hidden), where hidden is true when
    the command or any group above it is hidden."""
    out: list[tuple[str, object, bool]] = []
    for name, cmd in sorted(getattr(group, "commands", {}).items()):
        here = f"{path} {name}".strip()
        hidden = hidden_above or bool(getattr(cmd, "hidden", False))
        if hasattr(cmd, "commands"):
            out.extend(leaves(cmd, here, hidden))
        else:
            out.append((here, cmd, hidden))
    return out


def plumbing(root) -> list[tuple[str, str]]:
    """(path, one-line help) for every hidden command that `gr2 help -a` lists, in path order."""
    rows = []
    for path, cmd, hidden in leaves(root):
        if hidden and path not in _NOT_LISTED and path.split()[0] not in COMPAT_GROUPS:
            rows.append((path, cmd.get_short_help_str(limit=80)))
    return rows

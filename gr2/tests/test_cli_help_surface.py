"""The help surface: `gr2 --help` shows the porcelain, `gr2 help -a` lists the plumbing, and every path
still runs by its full name.

The porcelain is what a reader or an agent types in the walkthrough; plumbing is what scripts, CI and
other gr2 verbs call, registered with hidden=True. python_cli/surface.py holds the compat paths (hidden,
listed nowhere, still running). PORCELAIN below is the whole visible set on purpose: a new visible command
fails test_the_visible_commands_are_exactly_the_porcelain until it is placed here, and a new hidden one is
plumbing unless surface.COMPAT_PATHS names it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import typer.main
from typer.testing import CliRunner

from gr2.python_cli import surface
from gr2.python_cli.app import app

PORCELAIN = frozenset({
    "add",
    "branch",
    "check run",
    "check set",
    "check show",
    "commit",
    "lane create",
    "lane enter",
    "lane exit",
    "lane list",
    "lane push",
    "lane remove",
    "pr create",
    "pr merge",
    "pr status",
    "pr view",
    "push",
    "review close",
    "review merge",
    "review open",
    "review pin",
    "review publish",
    "review receive",
    "review run",
    "review show",
    "review stamp",
    "review update",
    "status",
    "store check",
    "store commit",
    "store init",
    "store materialize",
    "store push",
    "store status",
    "sync run",
    "sync status",
    "update",
})
TOP_LEVEL = frozenset(p.split()[0] for p in PORCELAIN)

# `gr2 <group> <word>` in printed text where <word> is prose, not a subcommand (read at the time of writing:
# a noun such as "a gr2 workspace spec", a stash message, a hook's own log prefix). Anything else that names a
# group must name one of its subcommands, so a renamed path cannot leave an instruction pointing nowhere.
KNOWN_PROSE = frozenset({
    ("exec", "runs"),
    ("grip", "store"),
    ("store", "hook"),
    ("sync", "auto-stash"),
    ("workspace", "commit"),
    ("workspace", "is"),
    ("workspace", "spec"),
})

SOURCE = Path(__file__).resolve().parents[1] / "gr2" / "python_cli"


def _root():
    return typer.main.get_command(app)


def _flat(text: str) -> str:
    return re.sub(r"[│╭╮╰╯─\s]+", " ", text)


def _listed_commands(text: str) -> set[str]:
    """Command names in the rows of a rendered help's command panel."""
    return set(re.findall(r"^│ ([a-z][a-z0-9-]*) ", text, re.M))


def test_the_visible_commands_are_exactly_the_porcelain() -> None:
    visible = {p for p, _, hidden in surface.leaves(_root()) if not hidden}
    assert visible == PORCELAIN, (
        f"visible but not porcelain: {sorted(visible - PORCELAIN)}; "
        f"porcelain but hidden or gone: {sorted(PORCELAIN - visible)}"
    )
    top = {name for name, cmd in _root().commands.items() if not getattr(cmd, "hidden", False)}
    assert top == TOP_LEVEL and len(top) == 12, sorted(top)


def test_help_and_bare_gr2_list_the_porcelain_and_no_plumbing() -> None:
    hidden_top = {name for name, cmd in _root().commands.items() if getattr(cmd, "hidden", False)}
    assert {"workspace", "grip", "hooks", "config", "plan", "apply", "help"} <= hidden_top
    for args in (["--help"], []):
        result = CliRunner().invoke(app, args, terminal_width=200)
        listed = _listed_commands(result.output)
        assert listed == set(TOP_LEVEL), (args, sorted(listed))


def test_help_a_lists_every_plumbing_path_and_nothing_else() -> None:
    expected = {p for p, _, hidden in surface.leaves(_root()) if hidden} - surface.COMPAT_PATHS - {"help"}
    expected = {p for p in expected if p.split()[0] not in surface.COMPAT_GROUPS}
    assert len(expected) >= 55, f"the walk found only {len(expected)} plumbing paths"
    for flag in ("-a", "--all"):
        result = CliRunner().invoke(app, ["help", flag], terminal_width=200)
        assert result.exit_code == 0, result.output
        section = result.output.split("Plumbing (hidden from gr2 --help", 1)[1]
        listed = set(re.findall(r"^  gr2 (\S+(?: \S+)*?) {2,}", section, re.M))
        assert listed == expected, (flag, sorted(listed ^ expected))
    plain = CliRunner().invoke(app, ["help"], terminal_width=200)
    assert plain.exit_code == 0 and "Plumbing (hidden" not in plain.output


def test_every_hidden_path_still_runs_by_its_full_name() -> None:
    hidden = [p for p, _, h in surface.leaves(_root()) if h and p != "help"]
    assert set(surface.COMPAT_PATHS) <= set(hidden), sorted(set(surface.COMPAT_PATHS) - set(hidden))
    for path in hidden:
        result = CliRunner().invoke(app, path.split() + ["--help"], terminal_width=200)
        assert result.exit_code == 0 and "Usage:" in result.output, (path, result.output[-300:])


def test_compat_paths_are_listed_nowhere() -> None:
    listing = CliRunner().invoke(app, ["help", "-a"], terminal_width=200).output
    for path in surface.COMPAT_PATHS:
        assert f"gr2 {path} " not in listing, path


def test_pr_merge_and_review_merge_each_name_the_other() -> None:
    root = _root()
    review_merge = root.commands["review"].commands["merge"]
    pr_merge = root.commands["pr"].commands["merge"]
    assert "pr merge" in review_merge.short_help and "review merge" in pr_merge.short_help
    for args, other in ((["review", "--help"], "use pr merge"), (["pr", "--help"], "use review merge"),
                        (["review", "merge", "--help"], "gr2 pr merge"), (["pr", "merge", "--help"], "gr2 review merge")):
        out = _flat(CliRunner().invoke(app, args, terminal_width=200).output)
        assert other in out, (args, other)


def test_printed_gr2_paths_name_real_subcommands() -> None:
    root = _root()
    unresolved = set()
    for source in sorted(SOURCE.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for m in re.finditer(r"(?<![\w/.-])gr2 ([a-z][a-z0-9-]*) ([a-z][a-z0-9-]*)", node.value):
                group = root.commands.get(m.group(1))
                if group is None or not hasattr(group, "commands"):
                    continue
                if m.group(2) not in group.commands and (m.group(1), m.group(2)) not in KNOWN_PROSE:
                    unresolved.add((source.name, m.group(0)))
    assert not unresolved, sorted(unresolved)


# Promised stable for 2.x when `gr2 --help` began showing them: the store verbs that keep their names through
# the planned renames, store init's workspace_root argument, and the store exit table. Renaming or removing
# one breaks a public promise, so it fails here first. `store commit` is to be renamed, so it must NOT be stable.
STABLE_STORE_ITEMS = frozenset({
    "verb store init",
    "verb store status",
    "verb store check",
    "verb store push",
    "verb store materialize",
    "arg store init workspace_root <str>",
    "exit store 0 ok",
    "exit store 2 usage",
    "exit store 3 refused-coverage",
    "exit store 4 refused-inconsistent",
    'exit store 5 cannot-measure prefix:"cannot complete this store verb: "',
})


def _dump_markers() -> dict[str, str]:
    markers = {}
    for line in (SOURCE.parents[1] / "api" / "cli.api").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        kind, marker = line.split()[0], line.split()[-1]
        label = line[len(kind):].rstrip()[: -len(marker)].strip()
        markers[f"{kind} {label}"] = marker
    return markers


def test_the_promised_store_items_exist_and_are_stable() -> None:
    markers = _dump_markers()
    missing = sorted(i for i in STABLE_STORE_ITEMS if i not in markers)
    assert not missing, f"a promised stable item was renamed or removed: {missing}"
    not_stable = sorted(i for i in STABLE_STORE_ITEMS if markers[i] != "stable")
    assert not not_stable, f"a promised item is no longer marked stable: {not_stable}"
    for verb in ("init", "status", "check", "push", "materialize"):
        assert f"store {verb}" in PORCELAIN, verb
    assert markers.get("verb store commit") == "may-change", "store commit is to be renamed; it must not be stable"


def test_help_a_lists_no_path_under_a_registered_alias_group() -> None:
    # `grip` is the registered alias of `store` ("alias grip" in api/deprecations.toml): help -a lists none of it.
    listing = CliRunner().invoke(app, ["help", "-a"], terminal_width=200).output
    assert not re.findall(r"^  gr2 grip ", listing, re.M), re.findall(r"^  gr2 grip \S+", listing, re.M)

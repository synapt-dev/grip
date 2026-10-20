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

import pytest
import typer.main
from typer import rich_utils
from typer.testing import CliRunner

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

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
    "store add",
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


def _help(args: list[str]) -> tuple[int, str]:
    """Exit code and rendered output with any styling removed.

    CI forces a styled terminal, so a row that reads the raw output finds no names
    there; every row that looks for names in rendered help takes the `colored` fixture
    and runs both ways. A row that only checks an exit code or "Usage:" runs once.
    """
    result = CliRunner().invoke(app, args, terminal_width=200)
    return result.exit_code, strip_ansi(result.output)


@pytest.fixture(params=[False, True], ids=["plain", "styled"])
def colored(request, monkeypatch) -> bool:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", request.param)
    return request.param


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


def test_help_and_bare_gr2_list_the_porcelain_and_no_plumbing(colored: bool) -> None:
    hidden_top = {name for name, cmd in _root().commands.items() if getattr(cmd, "hidden", False)}
    assert {"workspace", "grip", "hooks", "config", "plan", "apply", "help"} <= hidden_top
    for args in (["--help"], []):
        _, output = _help(args)
        listed = _listed_commands(output)
        assert listed == set(TOP_LEVEL), (args, sorted(listed))


def test_help_a_lists_every_plumbing_path_and_nothing_else(colored: bool) -> None:
    expected = {p for p, _, hidden in surface.leaves(_root()) if hidden} - surface.COMPAT_PATHS - {"help"}
    expected = {p for p in expected if not surface.under_compat_group(p)}
    assert len(expected) >= 55, f"the walk found only {len(expected)} plumbing paths"
    for flag in ("-a", "--all"):
        code, output = _help(["help", flag])
        assert code == 0, output
        section = output.split("Plumbing (hidden from gr2 --help", 1)[1]
        listed = set(re.findall(r"^  gr2 (\S+(?: \S+)*?) {2,}", section, re.M))
        assert listed == expected, (flag, sorted(listed ^ expected))
    code, output = _help(["help"])
    assert code == 0 and "Plumbing (hidden" not in output


def test_every_hidden_path_still_runs_by_its_full_name() -> None:
    hidden = [p for p, _, h in surface.leaves(_root()) if h and p != "help"]
    assert set(surface.COMPAT_PATHS) <= set(hidden), sorted(set(surface.COMPAT_PATHS) - set(hidden))
    for path in hidden:
        code, output = _help(path.split() + ["--help"])
        assert code == 0 and "Usage:" in output, (path, output[-300:])


def test_compat_paths_are_listed_nowhere(colored: bool) -> None:
    _, listing = _help(["help", "-a"])
    for path in surface.COMPAT_PATHS:
        assert f"gr2 {path} " not in listing, path


def test_pr_merge_and_review_merge_each_name_the_other(colored: bool) -> None:
    root = _root()
    review_merge = root.commands["review"].commands["merge"]
    pr_merge = root.commands["pr"].commands["merge"]
    assert "pr merge" in review_merge.short_help and "review merge" in pr_merge.short_help
    for args, other in ((["review", "--help"], "use pr merge"), (["pr", "--help"], "use review merge"),
                        (["review", "merge", "--help"], "gr2 pr merge"), (["pr", "merge", "--help"], "gr2 review merge")):
        out = _flat(_help(args)[1])
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


def test_help_a_lists_no_path_under_a_registered_alias_group(colored: bool) -> None:
    # `grip` is the registered alias of `store` ("alias grip" in api/deprecations.toml): help -a lists none of it.
    _, listing = _help(["help", "-a"])
    assert not re.findall(r"^  gr2 grip ", listing, re.M), re.findall(r"^  gr2 grip \S+", listing, re.M)


# `gr2 help <path>` is git's `git help <command>`: it prints that path's own --help, so the habit
# works. A group, a leaf and a hidden path each print exactly what `<path> --help` prints; an
# unknown path is refused by name.
FORWARDED = [
    ["review"], ["review", "merge"], ["store", "materialize"], ["workspace", "materialize"],
]


@pytest.mark.parametrize("path", FORWARDED, ids=lambda p: "-".join(p))
def test_help_path_prints_that_paths_own_help(path: list[str], colored: bool) -> None:
    code, forwarded = _help(["help", *path])
    direct_code, direct = _help([*path, "--help"])
    assert direct_code == 0 and "Usage:" in direct, direct[-300:]
    assert code == 0 and forwarded == direct, (path, forwarded[-400:])


def test_help_refuses_a_path_that_is_not_a_command() -> None:
    for path in (["nosuch"], ["review", "nosuch"], ["status", "extra"]):
        code, output = _help(["help", *path])
        assert code == 2, (path, code, output[-300:])
        flat = _flat(output)
        named = f"no gr2 command '{' '.join(path)}'" in flat
        assert named and "gr2 help -a" in flat, (path, flat[-300:])


def test_help_refusal_escapes_control_bytes_in_the_path() -> None:
    code, output = _help(["help", "review", "\x1b[31mRED"])
    assert code == 2, (code, output[-300:])
    assert "\x1b" not in output, repr(output[-300:])
    assert "no gr2 command 'review \\x1b[31mRED'" in _flat(output), repr(_flat(output)[-300:])


def test_help_refuses_all_with_a_path() -> None:
    code, output = _help(["help", "-a", "review"])
    assert code == 2 and "gr2 help -a" in _flat(output), output[-300:]
    assert "Plumbing (hidden" not in output, output[-300:]


def test_an_alias_group_at_any_depth_is_listed_nowhere(monkeypatch) -> None:
    # Only `grip` is registered today, at the top. An alias group registered BELOW the top (here
    # `lane old`) must be listed nowhere too, so the check reads every prefix of a path, not only
    # its first word.
    # Built the way the real CLI is, with Typer apps, so the row needs no separate `click` install
    # (a fresh `.[dev]` install has none, and Typer's own copy has no Group).
    def leaf() -> None:
        """A leaf."""

    old = typer.Typer()
    old.command("x")(leaf)
    old.command("z")(leaf)
    lane = typer.Typer()
    lane.add_typer(old, name="old", hidden=True)
    lane.command("y", hidden=True)(leaf)
    lane.command("w")(leaf)
    cli = typer.Typer()
    cli.add_typer(lane, name="lane")
    cli.command("top")(leaf)
    root = typer.main.get_command(cli)
    monkeypatch.setattr(surface, "COMPAT_GROUPS", frozenset({"grip", "lane old"}))
    listed = {path for path, _ in surface.plumbing(root)}
    assert listed == {"lane y"}, sorted(listed)

"""Doc-lint witness: every gr2 verb the README names exists in the CLI tree.

A README that names a verb gr2 does not have rots silently — the reader trusts
it, tries it, and gets a usage error. This witness makes the README's command
surface checkable: it reads gr2/README.md, extracts every ``gr2 <verb>
[<subverb>]`` invocation (from fenced bash blocks and inline code spans) plus
every group and verb named in the Commands table, and asserts each resolves in
the typer app's registered command tree.

A verb removed from the CLI, or a README line typed from memory, reds here.
"""

from __future__ import annotations

import re
from pathlib import Path

from gr2.python_cli.app import app

README = Path(__file__).resolve().parents[1] / "README.md"


def _command_tree() -> tuple[set[str], dict[str, set[str]]]:
    """(top-level names, {group: {subcommand names}}) from the typer app.

    A group's children are its commands AND its nested sub-groups (`lane lease`
    is a sub-typer, so it is addressable as `gr2 lane lease ...` and the README
    may name it).
    """
    top: set[str] = set()
    groups: dict[str, set[str]] = {}
    for info in app.registered_groups:
        assert info.name is not None
        subcommands: set[str] = set()
        for cmd in info.typer_instance.registered_commands:
            name = cmd.name or (cmd.callback.__name__.replace("_", "-") if cmd.callback else "")
            subcommands.add(name)
        for nested in info.typer_instance.registered_groups:
            if nested.name is not None:
                subcommands.add(nested.name)
        groups[info.name] = subcommands
        top.add(info.name)
    for cmd in app.registered_commands:
        name = cmd.name or (cmd.callback.__name__.replace("_", "-") if cmd.callback else "")
        top.add(name)
    return top, groups


def _fenced_bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def _inline_spans(text: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", text)


def _invocations(text: str) -> list[tuple[str, ...]]:
    """Every ``gr2 a [b]`` command path the README actually shows."""
    found: list[tuple[str, ...]] = []
    lines: list[str] = []
    for block in _fenced_bash_blocks(text):
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("gr2 "):
                lines.append(line)
    for span in _inline_spans(text):
        span = span.strip()
        if span.startswith("gr2 "):
            lines.append(span)
    for line in lines:
        tokens = [t for t in line.split() if not t.startswith("-")]
        if len(tokens) < 2 or tokens[0] != "gr2":
            continue
        # The first token after `gr2` is a command or a group; a second token is
        # a subcommand only when the first is a group (a following path/arg is
        # not a verb).
        path = (tokens[1],)
        if len(tokens) > 2:
            path = (tokens[1], tokens[2])
        found.append(path)
    return found


def _table_entries(text: str) -> list[tuple[str, str]]:
    """(group, verb) pairs from the Commands table rows.

    A row looks like ``| `workspace` | init, init-from-topology, ... |``; the
    verb cells are plain comma-separated words.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|") or "`" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        group_cell = cells[0]
        if not (group_cell.startswith("`") and group_cell.endswith("`")):
            continue
        group = group_cell.strip("`").strip()
        for cell in cells[1:]:
            for verb in cell.split(","):
                verb = verb.strip()
                if verb and re.fullmatch(r"[a-z][a-z0-9-]*", verb):
                    pairs.append((group, verb))
    return pairs


def test_every_readme_gr2_invocation_exists() -> None:
    text = README.read_text()
    top, groups = _command_tree()
    bad: list[str] = []
    for path in _invocations(text):
        head = path[0]
        if head in groups:
            if len(path) == 2 and path[1] not in groups[head]:
                bad.append(f"gr2 {path[0]} {path[1]} (no such subcommand)")
            continue
        if head not in top:
            bad.append(f"gr2 {path[0]} (no such command)")
    assert not bad, "README names verbs the CLI does not have:\n  " + "\n  ".join(sorted(set(bad)))


def test_every_readme_table_group_and_verb_exists() -> None:
    text = README.read_text()
    top, groups = _command_tree()
    bad: list[str] = []
    for group, verb in _table_entries(text):
        if group == "(top level)":
            if verb not in top:
                bad.append(f"{verb} (no such top-level command)")
            continue
        # A row may name several groups (`target`, `config`, ...).
        for one in [g.strip() for g in group.split(",")]:
            one = one.strip().strip("`").strip()
            if one not in groups:
                bad.append(f"group {one} (no such group)")
            elif verb not in groups[one]:
                bad.append(f"gr2 {one} {verb} (no such subcommand)")
    assert not bad, "README table names things the CLI does not have:\n  " + "\n  ".join(sorted(set(bad)))


def test_the_witness_actually_reads_the_readme() -> None:
    # Guard against a silent pass over an empty extraction: the README must
    # yield invocations and table entries, or this file proves nothing.
    text = README.read_text()
    invocations = _invocations(text)
    entries = _table_entries(text)
    assert len(invocations) >= 8, invocations
    assert len(entries) >= 20, entries

"""The --actor examples a user reads name no real agent or person.

Each help text and the no-actor refusal give the label's shape, `<kind>:<name>`, as the example. A row
reads the text as a user sees it: the rendered `--help` of the verb, or the message the refusal raises.
"""
from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

from gr2.python_cli.app import app
from gr2.python_cli.context import ActorRefused, resolve_actor

PLACEHOLDER = "<kind>:<name>"
# A real-looking label: a kind, a colon, then a lower-case name. The placeholder cannot match, since
# `<` is not a lower-case letter.
CONCRETE_LABEL = re.compile(r"\b(?:agent|human):[a-z][a-z0-9_-]*")


def _help(path: list[str]) -> str:
    result = CliRunner().invoke(app, path + ["--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    return re.sub(r"[│╭╮╰╯─\s]+", " ", strip_ansi(result.output))


def _assert_example(text: str) -> None:
    assert PLACEHOLDER in text, text
    found = CONCRETE_LABEL.findall(text)
    assert not found, f"a concrete label in the example text: {found}"


@pytest.mark.parametrize("path", [["exec", "run"], ["lane", "enter"], ["lane", "resolve"], ["lane", "exit"]],
                         ids=["exec-run", "lane-enter", "lane-resolve", "lane-exit"])
def test_actor_help_example_is_the_placeholder(path):
    _assert_example(_help(path))


def test_no_actor_refusal_example_is_the_placeholder():
    with pytest.raises(ActorRefused) as refused:
        resolve_actor(None, env={}, tty=False)
    _assert_example(str(refused.value))

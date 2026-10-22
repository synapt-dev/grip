"""The value gr2 prints after 'pass --branch' / '--repo-path' in a lane error is the one the shell hands back.

Each printed fragment is evaluated from app.py's own source with a value holding shell text; shlex.split of the
fragment must give back that exact value.
"""
from __future__ import annotations

import ast
import shlex
from pathlib import Path

import pytest

from gr2.python_cli import next_steps

APP = Path(__file__).resolve().parents[1] / "gr2" / "python_cli" / "app.py"
FLAGS = ("pass --branch ", "--repo-path ")


def _holes(flag: str) -> list[ast.expr]:
    found = []
    for node in ast.walk(ast.parse(APP.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.JoinedStr):
            continue
        for before, part in zip(node.values, node.values[1:]):
            if isinstance(before, ast.Constant) and str(before.value).endswith(flag) and isinstance(part, ast.FormattedValue):
                found.append(part.value)
    return found


@pytest.mark.parametrize("flag", FLAGS)
def test_a_value_with_shell_text_round_trips_through_the_printed_flag(flag: str) -> None:
    holes = _holes(flag)
    assert holes, f"no printed '{flag}{{...}}' in app.py"
    value = "a b;c"
    for expr in holes:
        shown = eval(compile(ast.Expression(expr), str(APP), "eval"), {"next_steps": next_steps, "str": str},
                     {"branch": value, "top": value})
        assert shlex.split(f"{flag.split()[-1]} {shown}") == [flag.split()[-1], value]

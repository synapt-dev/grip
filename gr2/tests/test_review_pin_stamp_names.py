"""`review pin`, `stamp` and `repin` are the names; `bind`, `approve` and `rebind` stay as aliases of them.

An alias that is a second copy drifts: one name grows an option and the other does not. These rows hold the
pair to ONE command object, the same options, and the same output on the same input.
"""
from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()
PAIRS = [("pin", "bind"), ("stamp", "approve"), ("repin", "rebind")]


def _review_group():
    return typer.main.get_command(app).commands["review"]


@pytest.mark.parametrize("name,alias", PAIRS)
def test_alias_runs_the_same_function_with_the_same_options(name, alias):
    from gr2.python_cli.app import review_app
    registered = {info.name: info.callback for info in review_app.registered_commands}
    assert registered[alias] is registered[name], f"{alias} must run the function {name} runs"
    group = _review_group()
    canonical, old = group.commands[name], group.commands[alias]
    assert [p.name for p in old.params] == [p.name for p in canonical.params]
    assert type(old) is type(canonical)


@pytest.mark.parametrize("name,alias", PAIRS)
def test_alias_help_points_at_the_name(name, alias):
    result = runner.invoke(app, ["review", alias, "--help"])
    assert result.exit_code == 0, result.output
    assert f"Alias of `review {name}`" in result.output


@pytest.mark.parametrize("name,alias", PAIRS)
def test_name_and_alias_give_the_same_output_on_the_same_input(name, alias, tmp_path, monkeypatch):
    # An empty directory is no workspace: each verb refuses it, and the refusal must not depend on the spelling.
    monkeypatch.chdir(tmp_path)
    args = {"pin": [str(tmp_path)], "stamp": [str(tmp_path)], "repin": [str(tmp_path / "frozen"), "--repo", str(tmp_path)]}[name]
    first = runner.invoke(app, ["review", name, *args])
    second = runner.invoke(app, ["review", alias, *args])
    assert first.exit_code == second.exit_code
    assert first.exit_code != 0, first.output
    # The usage line names the spelling that was typed; everything else must match.
    assert first.output == second.output.replace(f"review {alias}", f"review {name}")


def test_review_help_lists_each_name_with_its_alias():
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == 0, result.output
    for name, alias in PAIRS:
        assert name in result.output and f"Alias of `review {name}`" in result.output

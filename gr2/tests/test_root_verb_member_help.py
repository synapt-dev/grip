"""The help of each verb that refuses inside a member names that refusal, in the git word the refusal prints.

Inside a member checkout of a workspace root, add, commit and push refuse at exit 2 and point at git (the verb
table: inside a member you use git). An agent reads --help before it types, so the help says the same thing the
refusal does. The rows walk root_verbs.REFUSE_IN_MEMBER, so a verb added there without its help sentence fails,
and they drive the refusal on both root shapes (a full-mode griplink root and an older-shape store root).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from typer import rich_utils
from typer.testing import CliRunner

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

from gr2.python_cli import root_verbs
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}

ARGS = {"add": ("add", "a.txt"), "commit": ("commit", "-m", "inside"), "push": ("push",)}


@pytest.fixture(params=[False, True], ids=["plain", "styled"])
def colored(request, monkeypatch) -> bool:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", request.param)
    return request.param


def _help(verb: str) -> str:
    result = CliRunner().invoke(app, [verb, "--help"], terminal_width=200)
    assert result.exit_code == 0, result.output
    return re.sub(r"[│╭╮╰╯─\s]+", " ", strip_ansi(result.output))


def test_the_walk_covers_add_commit_and_push():
    # Control: the rows below walk this tuple, so they are empty if it is.
    assert set(root_verbs.REFUSE_IN_MEMBER) == set(ARGS)


@pytest.mark.parametrize("verb", root_verbs.REFUSE_IN_MEMBER)
def test_help_names_the_member_refusal_and_its_setting(verb, colored):
    text = _help(verb)
    assert f"Inside a member checkout of a workspace root, use git {verb} there" in text, text
    assert f"gr2 {verb} refuses at exit 2" in text, text
    assert 'inside_member = "root"' in text, text


@pytest.mark.parametrize("verb", root_verbs.REFUSE_IN_MEMBER)
def test_help_carries_no_card_number_or_post_id(verb, colored):
    text = _help(verb)
    assert "2.174" not in text and "m_" not in text, text


def test_commit_help_scopes_the_root_commit_to_outside_the_members(colored):
    text = _help("commit")
    assert "At a workspace root (outside its members), commits the root" in text, text
    assert "or below one" not in text, text


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(params=["griplink", "store"])
def member(request, tmp_path: Path, monkeypatch) -> Path:
    """A member checkout `api` under a root of each shape; returns the member's path."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("v1\n")
    _git(src, "add", "a.txt")
    _git(src, "commit", "-q", "-m", "base")
    url = tmp_path / "api.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    monkeypatch.chdir(tmp_path)
    if request.param == "griplink":
        r = make_cli_runner().invoke(app, ["store", "init", "--mode", "full", "R"])
        assert r.exit_code == 0, r.stderr
        monkeypatch.chdir(tmp_path / "R")
        r = make_cli_runner().invoke(app, ["add", str(url), "api"])
    else:
        r = make_cli_runner().invoke(app, ["store", "init", "--clone", f"api={url}", "R"])
    assert r.exit_code == 0, r.stderr
    found = root_verbs.find(tmp_path / "R" / "api")
    assert found is not None and found.shape == request.param and found.member == "api", found
    (tmp_path / "R" / "api" / "a.txt").write_text("v2\n")
    return tmp_path / "R" / "api"


@pytest.mark.parametrize("verb", root_verbs.REFUSE_IN_MEMBER)
def test_the_refusal_inside_a_member_is_the_one_the_help_names(verb, member, monkeypatch):
    monkeypatch.chdir(member)
    r = make_cli_runner().invoke(app, list(ARGS[verb]))
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert f"use git {verb} here" in r.stderr, r.stderr
    assert str(member.parent) in r.stderr, r.stderr

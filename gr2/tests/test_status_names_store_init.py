"""`gr2 status` in a workspace with no store names today's step, `gr2 store init`.

A second workspace (a member clone and no store) used to be told only that it has no workspace spec. The refusal and
its exit code are unchanged; the line now ends by naming the step that makes the directory a store. Inside a member
checkout it does not say to init there, an empty directory is told init needs a member checkout, and a griplink
root (a plain clone of a full-mode root, found by its gitlink alone) keeps its own refusal text byte for byte.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}

# The griplink refusal as `gr2 status` printed it before this change.
GRIPLINK_REFUSAL = ("gr2 status: this workspace's names were not fetched (a plain git clone has main only); run gr2 "
                    "checkout (or gr2 pull), then gr2 status again")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def status(cwd: Path, monkeypatch):
    monkeypatch.chdir(cwd)
    r = make_cli_runner().invoke(app, ["status"])
    raw = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
    text = " ".join(re.sub("[│╭╮╰╯─]", " ", raw).split())
    print(f"rc={r.exit_code} {text}")
    return r, text


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def second_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws2"
    (ws / "app").mkdir(parents=True)
    git(ws / "app", "init", "-q", "-b", "main")
    git(ws / "app", "commit", "-q", "--allow-empty", "-m", "base")
    return ws


def test_r1_second_workspace_names_store_init(second_workspace, monkeypatch):
    r, text = status(second_workspace, monkeypatch)
    assert r.exit_code == 2
    assert text.count("has none") == 1
    assert text.count("gr2 store init") == 1


def test_r2_inside_member_does_not_say_store_init_here(second_workspace, monkeypatch):
    r, text = status(second_workspace / "app", monkeypatch)
    assert r.exit_code == 2
    assert text.count("store init .") == 0


def test_r3_empty_directory_needs_a_member_checkout(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    r, text = status(empty, monkeypatch)
    assert r.exit_code == 2
    assert text.count("needs at least one member checkout") == 1


def test_r4_griplink_root_refusal_is_unchanged(tmp_path, monkeypatch):
    """Built the way test_griplink_cli.py's `world` builds a full-mode root; then a plain clone of it, which holds the
    <m>.griplink/001.2_pin gitlink and no STOOK."""
    src = tmp_path / "src-api"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("v1\n")
    git(src, "add", "a.txt")
    git(src, "commit", "-q", "-m", "base")
    url = tmp_path / "api.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    monkeypatch.chdir(tmp_path)
    assert make_cli_runner().invoke(app, ["store", "init", "--mode", "full", "R"]).exit_code == 0
    monkeypatch.chdir(tmp_path / "R")
    assert make_cli_runner().invoke(app, ["add", str(url), "api"]).exit_code == 0
    git(tmp_path, "clone", "-q", str(tmp_path / "R"), str(tmp_path / "C"))
    assert git(tmp_path / "C", "ls-tree", "HEAD", "api.griplink/001.2_pin").startswith("160000 commit ")
    r, text = status(tmp_path / "C", monkeypatch)
    assert GRIPLINK_REFUSAL is not None
    assert r.exit_code == 4
    assert text == GRIPLINK_REFUSAL

"""`review publish` and `review merge` at a full-mode root refuse with the full-mode sentence, and push nothing.

A full-mode root (`store init --mode full`) has no grip.toml and no .grip/ store. The review verbs read that store,
so on such a root they refuse; the refusal names the root's kind and points at `gr2 status`, the verb that does read
it, instead of offering an init on a root that is already a workspace. These rows pin that refusal from the root and
from inside a member, and read the member's remote back to show the refusal left it as it was.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "app-src"
    _git("init", "-q", "-b", "main", str(src))
    (src / "a.txt").write_text("v1\n")
    _git("-C", str(src), "add", "a.txt")
    _git("-C", str(src), "commit", "-q", "-m", "base")
    bare = tmp_path / "app.git"
    _git("clone", "-q", "--bare", str(src), str(bare))
    monkeypatch.chdir(tmp_path)
    r = make_cli_runner().invoke(app, ["store", "init", "--mode", "full", "--clone", f"app=file://{bare}", "root"])
    assert r.exit_code == 0, (r.stdout, r.stderr)
    root = (tmp_path / "root").resolve()
    assert not (root / "grip.toml").exists() and not (root / ".grip").exists()  # control: the full-mode shape
    return root, bare, _git("-C", str(bare), "rev-parse", "main"), _git("ls-remote", str(bare))


def _sentence(root: Path) -> str:
    return (f"{root} is a full-mode gr2 root (no grip.toml; members under <member>.griplink), and this "
            "verb does not read a full-mode root yet; `gr2 status` reads it")


@pytest.mark.parametrize("where, verb, code", [
    (".", ["review", "publish", "{sha}"], 2),
    (".", ["review", "publish", "{sha}", "--remote", "{bare}"], 2),
    (".", ["review", "merge", "--approvals", "0"], 3),
    ("app", ["review", "publish", "{sha}", "--remote", "{bare}"], 2),
], ids=["publish-at-root", "publish-remote-at-root", "merge-at-root", "publish-remote-from-member"])
def test_review_verb_at_a_full_root_refuses_with_the_full_mode_sentence(full, where, verb, code, monkeypatch):
    root, bare, sha, refs_before = full
    monkeypatch.chdir(root / where)
    argv = [a.format(sha=sha, bare=bare) for a in verb]
    r = make_cli_runner().invoke(app, argv)
    assert r.exit_code == code, (r.stdout, r.stderr)
    assert _sentence(root) in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert _git("ls-remote", str(bare)) == refs_before

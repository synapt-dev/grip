"""`review pin` records the author git itself would use: GIT_AUTHOR_NAME when set, else config user.name."""
# ruff: noqa: F811 -- imported pytest fixture is intentionally named in test arguments.
from __future__ import annotations

import subprocess

from gr2.python_cli import grip
from tests.test_review_transport import cli, git, handoff  # noqa: F401


def _pin(root, remote, base, head, monkeypatch):
    pinned = cli(root, monkeypatch, "pin", root, "--repo", "member", "--remote", remote, "--base", base,
                 "--head", head, "--ref", "refs/heads/main", "--source", root / "member")
    assert pinned.exit_code == 0, pinned.output
    return grip.show_review_commit(root, pinned.stdout.strip().splitlines()[-1][3:])["author"]


def test_env_author_wins_over_config_like_git(handoff, monkeypatch):
    root, _, remote, _, base, head = handoff
    git(root, "config", "user.name", "Config Name")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Name")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "env@example.invalid")
    ident = subprocess.run(["git", "var", "GIT_AUTHOR_IDENT"], cwd=root, capture_output=True, text=True)
    assert ident.stdout.startswith("Env Name <env@example.invalid>"), ident
    assert _pin(root, remote, base, head, monkeypatch) == "Env Name"


def test_no_env_author_uses_config_name(handoff, monkeypatch):
    root, _, remote, _, base, head = handoff
    git(root, "config", "user.name", "Config Name")
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    assert _pin(root, remote, base, head, monkeypatch) == "Config Name"

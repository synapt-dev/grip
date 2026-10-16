"""The PR base fetch writes nothing beyond FETCH_HEAD under a configured `remote.origin.fetch` refspec or `tagOpt=--tags`.

git-fetch(1), CONFIGURED REMOTE-TRACKING BRANCHES: `git fetch origin <branch>` also updates every ref the configured
refspecs map `<branch>` to, unless `--refmap=` is passed; `tagOpt=--tags` fetches every tag unless `--no-tags` is passed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from gr2.python_cli import app


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class Adapter:
    def __init__(self, base_branch="dev"):
        self.base_branch = base_branch

    def pr_status(self, repo, number):
        return SimpleNamespace(ref=SimpleNamespace(base_branch=self.base_branch))


def make_repos(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(work)], check=True)
    for n in ("one", "two"):
        (work / n).write_text(n)
        git(work, "add", n)
        git(work, "commit", "-qm", n)
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "dev")
    git(work, "branch", "keep", "HEAD~1")
    git(remote, "tag", "t1", "dev")
    return work, remote


def test_pr_base_fetch_writes_nothing(tmp_path, monkeypatch):
    work, remote = make_repos(tmp_path)
    keep, tip = git(work, "rev-parse", "keep"), git(work, "rev-parse", "dev")
    git(work, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/dev:refs/heads/keep")
    git(work, "config", "remote.origin.tagOpt", "--tags")
    monkeypatch.setattr(app.platform_ops, "get_platform_adapter", lambda platform: Adapter())

    assert app._pr_base_sha(work, "repo", 1, tip, "fake") == tip
    assert git(work, "rev-parse", "keep") == keep
    assert git(work, "tag", "-l") == ""


@pytest.mark.parametrize("base_branch", ["--upload-pack=touch {x}", "--upload-pack={hook}", "dev:refs/heads/keep", "refs/heads/dev:refs/tags/z", "-x"])
def test_pr_base_branch_that_is_not_a_plain_branch_is_refused(tmp_path, monkeypatch, base_branch):
    """The base branch name comes from the platform's PR answer: a leading `-` must not reach git as an option (`--upload-pack={hook}` passes
    check-ref-format, so only `--` or the `refs/heads/` prefix stops it) and
    a `:` must not reach it as a refspec that writes local refs."""
    work, _ = make_repos(tmp_path)
    marker, hook = tmp_path / "X", tmp_path / "hook"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    before = git(work, "for-each-ref")
    monkeypatch.setattr(app.platform_ops, "get_platform_adapter",
                        lambda platform: Adapter(base_branch.format(x=marker, hook=hook)))

    with pytest.raises(typer.Exit):
        app._pr_base_sha(work, "repo", 1, git(work, "rev-parse", "dev"), "fake")
    assert git(work, "for-each-ref") == before
    assert not marker.exists()


def test_pr_base_fetch_that_fails_is_refused_not_pinned_to_a_stale_fetch_head(tmp_path, monkeypatch):
    work, _ = make_repos(tmp_path)
    git(work, "fetch", "--quiet", "--refmap=", "--no-tags", "origin", "dev")
    monkeypatch.setattr(app.platform_ops, "get_platform_adapter", lambda platform: Adapter("gone"))

    with pytest.raises(typer.Exit):
        app._pr_base_sha(work, "repo", 1, git(work, "rev-parse", "dev"), "fake")

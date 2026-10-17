"""`check set` never prints a recorded remote that carries credentials.

A record made before the writers refused such a remote can still reach `check set`. When a member's clone lacks
its pinned head, the refusal names the fetch to run, and that command would carry the recorded remote. The set
check now refuses `remote_credentials` by member key before any message can name the remote. FAKE token only.
"""
from __future__ import annotations

import subprocess

from gr2.python_cli import grip
from gr2.python_cli.app import app
from tests.test_review_remote_safety import CRED, TOKEN, bind, git, reviews, runner, world, world_env  # noqa: F401


def test_a_set_check_on_a_credentialed_record_missing_its_head_never_prints_the_remote(world, monkeypatch):
    monkeypatch.setattr(grip, "_refuse_remote_credentials", lambda key, remote: None, raising=False)
    made = bind(world, monkeypatch, CRED)  # the writers refuse it now; this record stands for one made before
    assert made.exit_code == 0, made.output
    monkeypatch.undo()
    world_env(world, monkeypatch)
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    member = world["member"]
    git(member, "reset", "--hard", world["base"])
    git(member, "reflog", "expire", "--expire=now", "--all")
    git(member, "gc", "--prune=now", "-q")
    assert subprocess.run(["git", "cat-file", "-e", world["head"] + "^{commit}"], cwd=member).returncode != 0, \
        "precondition: the member clone lacks the pinned head"
    monkeypatch.chdir(world["author"])
    got = runner.invoke(app, ["check", "set", str(world["author"]), "gr:" + commit, "--", "true"])
    out = got.output + (got.stderr if got.stderr_bytes is not None else "")
    assert got.exit_code != 0, out
    assert "remote_credentials: member" in out, out
    assert TOKEN not in out and "user:" not in out, "the set check must never print the credential"

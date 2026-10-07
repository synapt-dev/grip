"""A review record never carries URL credentials, and an author-local path never leaves the host.

The credential guard used to live at ONE writer (`store init`), so a remote reaching a record by any
other route -- `review bind --remote`, a hand-edited grip.toml, a project review pin, a workspace commit --
was written verbatim and `review publish` pushed it. These tests hold every writer and the publisher.
FAKE token only; every fake https URL is rewritten to a local bare repo, and a dead proxy backs that up.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import grip
from gr2.python_cli.app import app

runner = CliRunner()
PREFIX = "refs/dev.synapt.grip/__reviews__/"
TOKEN = "FAKE-TOKEN-0000"
CRED = f"https://user:{TOKEN}@example.invalid/o/member.git"
FOREIGN = "https://example.invalid/o/destination.git"


def git(root, *args):
    p = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def review(root, monkeypatch, *args):
    monkeypatch.chdir(root)
    return runner.invoke(app, ["review", *map(str, args)])


def reviews(root):
    return git(root, "for-each-ref", "--format=%(refname)", PREFIX)


@pytest.fixture
def world(tmp_path, monkeypatch):
    remote, dest, seed, author = tmp_path / "remote.git", tmp_path / "dest.git", tmp_path / "seed", tmp_path / "author"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "file://{remote}"]\n\tinsteadOf = {CRED}\n[url "file://{dest}"]\n\tinsteadOf = {FOREIGN}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for bare in (remote, dest):
        git(tmp_path, "init", "--bare", "-b", "main", bare)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "payload.txt").write_text("base\n")
    git(seed, "add", "payload.txt")
    git(seed, "commit", "-m", "base")
    base = git(seed, "rev-parse", "HEAD")
    git(seed, "push", remote, "main")
    author.mkdir()
    git(author, "clone", "--no-local", "--branch", "main", remote, "member")
    init = runner.invoke(app, ["store", "init", str(author)])
    assert init.exit_code == 0, init.output
    member = author / "member"
    (member / "payload.txt").write_text("reviewed\n")
    git(member, "add", "payload.txt")
    git(member, "commit", "-m", "reviewed")
    return dict(author=author, member=member, remote=remote, dest=dest, base=base, head=git(member, "rev-parse", "HEAD"))


def bind(w, monkeypatch, remote):
    return review(w["author"], monkeypatch, "bind", "--repo", "member", "--remote", remote, "--base", w["base"],
                  "--head", w["head"], "--ref", "refs/heads/main", "--source", w["member"])


def assert_credential_refusal(result):
    assert result.exit_code != 0, result.output
    assert "credentials" in result.output, result.output
    assert TOKEN not in result.output, "a refusal must never print the credential"


def test_instrument_rewrites_the_fake_url_locally(world):
    # Control: the fake credential URL reaches the LOCAL bare repo, so a pass below is not a network artifact.
    assert "refs/heads/main" in git(world["author"], "ls-remote", CRED)


def test_bind_refuses_credential_remote_given_by_flag(world, monkeypatch):
    assert_credential_refusal(bind(world, monkeypatch, CRED))
    assert reviews(world["author"]) == ""


def test_bind_refuses_credential_remote_from_the_manifest(world, monkeypatch):
    toml = world["author"] / "grip.toml"
    toml.write_text(toml.read_text().replace(str(world["remote"]), CRED))
    assert TOKEN in toml.read_text()
    assert_credential_refusal(review(world["author"], monkeypatch, "bind"))
    assert reviews(world["author"]) == ""


def test_publish_refuses_an_existing_record_that_carries_credentials(world, monkeypatch):
    # A record made before the writer guard existed (or received from elsewhere) must not travel.
    monkeypatch.setattr(grip, "_refuse_remote_credentials", lambda key, remote: None, raising=False)
    made = bind(world, monkeypatch, CRED)
    assert made.exit_code == 0, made.output
    monkeypatch.undo()
    world_env(world, monkeypatch)
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    assert TOKEN in git(world["author"], "show", f"{commit}:repos/member/remote")
    assert_credential_refusal(review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", world["dest"]))
    assert git(world["dest"], "for-each-ref") == ""


def test_publish_keeps_an_author_path_on_the_host(world, monkeypatch):
    made = bind(world, monkeypatch, str(world["remote"]))
    assert made.exit_code == 0, made.output
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    off_host = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", FOREIGN)
    assert off_host.exit_code != 0, off_host.output
    assert "local path" in off_host.output, off_host.output
    assert git(world["dest"], "for-each-ref") == ""
    # The same record to a LOCAL destination is the supported on-host handoff and still publishes.
    on_host = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", world["dest"])
    assert on_host.exit_code == 0, on_host.output


def test_project_review_writer_refuses_credentials(world):
    pin = dict(key="member", repo=CRED, path="member", head=world["head"], base=world["base"])
    with pytest.raises(grip.GripReviewRefused) as caught:
        grip.create_project_review_commit(world["author"], [pin])
    assert caught.value.refusal == "remote_credentials" and TOKEN not in str(caught.value)


def test_workspace_commit_writer_refuses_credentials(world, tmp_path):
    # This writer lives in the alpha snapshot store (`.grip/.git`), not a native root.
    alpha = tmp_path / "alpha"
    (alpha / ".grip").mkdir(parents=True)
    git(alpha / ".grip", "init", "-q")
    repo = dict(key="member", remote=CRED, path="member", commit=world["head"], base=world["base"])
    with pytest.raises(grip.GripReviewRefused) as caught:
        grip.create_workspace_commit(alpha, [repo])
    assert caught.value.refusal == "remote_credentials" and TOKEN not in str(caught.value)


def test_ssh_login_is_not_a_credential():
    assert not grip.url_has_credentials("ssh://git@github.com/o/r.git")
    assert grip.url_has_credentials(CRED)
    assert grip.url_has_credentials("https://user@example.invalid/o/r.git")


def world_env(world, monkeypatch):
    """monkeypatch.undo() also drops the env; restore the no-network instrument."""
    gitconfig = world["author"].parent / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")

"""`review merge` never prints a recorded remote that carries credentials.

The writers refuse such a remote, but a record made before they did (or by another writer) still reaches
`review merge`, which printed the remote in every receipt row, in the tracking advice and in each next step.
The merge now refuses before any row is built, naming the member and never the URL. An ssh login and a plain
path are not credentials and merge as before. FAKE token only; every URL is rewritten to a local bare repo.
"""
from __future__ import annotations

import json

import pytest

from gr2.python_cli import grip, merge_gate
from gr2.python_cli.app import app
from tests.test_review_remote_safety import bind, git, reviews, runner, world, world_env  # noqa: F401

TOKEN = "FAKE-TOKEN-1309"
CRED = f"https://x-access-token:{TOKEN}@nonexistent.invalid/o/r.git"
SSH_LOGIN = "git@github.com:o/r.git"


def _rewrite(w, url):
    gitconfig = w["author"].parent / "gitconfig"
    gitconfig.write_text(gitconfig.read_text() + f'[url "file://{w["remote"]}"]\n\tinsteadOf = {url}\n')


def _bound(w, monkeypatch, remote, *, credentialed=False):
    _rewrite(w, remote)
    if credentialed:  # the writers refuse it now; this record stands for one made before they did
        monkeypatch.setattr(grip, "_refuse_remote_credentials", lambda key, remote: None, raising=False)
    made = bind(w, monkeypatch, remote)
    assert made.exit_code == 0, made.output
    if credentialed:
        monkeypatch.undo()
        world_env(w, monkeypatch)
    return reviews(w["author"]).rsplit("/", 1)[1]


def _main(w):
    return git(w["remote"], "rev-parse", "refs/heads/main")


def test_merge_refuses_a_credentialed_record_without_printing_it(world, monkeypatch):
    commit = _bound(world, monkeypatch, CRED, credentialed=True)
    before = _main(world)
    code, receipt = merge_gate.review_merge(world["author"], commit, into="main", feature="main", no_set=True)
    text = json.dumps(receipt)
    assert code == merge_gate.EXIT_REFUSED, text
    assert receipt["refused"].startswith("remote_credentials: member"), text
    assert receipt["members"] == [], text
    assert TOKEN not in text and "x-access-token" not in text, "the receipt must never print the credential"
    assert _main(world) == before, "nothing may be merged"


def test_cli_merge_refuses_a_credentialed_record_without_printing_it(world, monkeypatch):
    commit = _bound(world, monkeypatch, CRED, credentialed=True)
    before = _main(world)
    monkeypatch.chdir(world["author"])
    got = runner.invoke(app, ["review", "merge", str(world["author"]), "gr:" + commit,
                              "--into", "main", "--from", "main", "--no-set"])
    out = got.output + (got.stderr if got.stderr_bytes is not None else "")
    assert got.exit_code == merge_gate.EXIT_REFUSED, out
    assert "remote_credentials: member" in out, out
    assert TOKEN not in out and "x-access-token" not in out, "the CLI must never print the credential"
    assert _main(world) == before, "nothing may be merged"


def test_control_a_clean_remote_reaches_the_member_rows(world, monkeypatch):
    commit = _bound(world, monkeypatch, "https://example.invalid/o/member.git")
    code, receipt = merge_gate.review_merge(world["author"], commit, into="main", feature="main", no_set=True)
    assert not str(receipt.get("refused") or "").startswith("remote_credentials"), receipt
    assert [m["key"] for m in receipt["members"]] == ["member"], receipt


@pytest.mark.parametrize("which", ["ssh-login", "plain-path"])
def test_an_ssh_login_and_a_plain_path_are_not_credentials(world, monkeypatch, which):
    # `user@host` is URL userinfo too; refusing it would refuse every ssh user.
    remote = SSH_LOGIN if which == "ssh-login" else str(world["remote"])
    assert not grip.url_has_credentials(remote), remote
    commit = _bound(world, monkeypatch, remote)
    code, receipt = merge_gate.review_merge(world["author"], commit, into="main", feature="main", no_set=True)
    assert not str(receipt.get("refused") or "").startswith("remote_credentials"), receipt
    assert [m["remote"] for m in receipt["members"]] == [remote], receipt

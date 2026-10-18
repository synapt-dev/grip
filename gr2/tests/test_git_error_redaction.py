"""A failed git command's error message names the command, never a credential carried in a remote URL.

GitError shows the command's first words, and for `ls-remote <remote>` the second word is the remote itself. git
strips userinfo from its own stderr; the argument echo has to do the same, or a refusal that quotes the error carries
the credential into a receipt."""
from __future__ import annotations

import dataclasses
import json
import subprocess

import pytest

from gr2.python_cli import declared_sets, gitops, merge_gate
from tests.test_declared_sets import merge, record, world  # noqa: F401  (fixture and helpers)
from tests.test_review_merge_gate import _git_identity  # noqa: F401  (fixture)

TOKEN = "FAKETOKEN"
USERINFO = f"https://x:{TOKEN}@127.0.0.1:9/a.git"
QUERY = f"https://127.0.0.1:9/a.git?access_token={TOKEN}"


def _with_remotes(monkeypatch, remotes):
    real = declared_sets.read

    def read(root, revision="HEAD"):
        declaration = real(root, revision)
        members = {name: {**info, "remote": remotes.get(name, info["remote"])} for name, info in declaration.members.items()}
        return dataclasses.replace(declaration, members=members)

    monkeypatch.setattr(declared_sets, "read", read)


def _merged_text(world, capsys):
    code, receipt = merge(world, "alpha")
    seen = capsys.readouterr()
    return code, receipt, seen.out + seen.err + json.dumps(receipt)


def test_a_userinfo_remote_is_not_shown_in_the_refusal(world, monkeypatch, capsys):
    assert record(world)["result"] == "pass"
    _with_remotes(monkeypatch, {"beta": USERINFO, "gamma": QUERY})

    code, receipt, text = _merged_text(world, capsys)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert "declared_set_pin_unmeasured" in json.dumps(receipt) or "ls-remote" in json.dumps(receipt), receipt
    assert text.count(TOKEN) == 0, text


def test_an_access_token_remote_is_not_shown_in_the_refusal(world, monkeypatch, capsys):
    assert record(world)["result"] == "pass"
    _with_remotes(monkeypatch, {"gamma": QUERY})

    code, receipt, text = _merged_text(world, capsys)

    assert code == merge_gate.EXIT_REFUSED and "ls-remote" in json.dumps(receipt), receipt
    assert text.count(TOKEN) == 0, text


def _error(*args):
    proc = subprocess.CompletedProcess(list(args), 128, b"", b"fatal: unable to access")
    with pytest.raises(gitops.GitError) as caught:
        gitops._raise(proc, args, None)
    return str(caught.value)


@pytest.mark.parametrize("remote", [USERINFO, QUERY])
def test_git_error_does_not_show_a_credential_in_a_remote(remote):
    shown = _error("ls-remote", remote, "refs/heads/main")

    assert shown.count(TOKEN) == 0, shown
    assert shown.startswith("git ls-remote "), shown


def test_git_error_keeps_the_host_of_a_userinfo_remote():
    assert _error("ls-remote", USERINFO) == "git ls-remote https://127.0.0.1:9/a.git: fatal: unable to access"


@pytest.mark.parametrize("args", [("ls-remote", "https://127.0.0.1:9/a.git"), ("ls-remote", "git@host:org/a.git"),
                                  ("rev-parse", "refs/heads/a#b"), ("cat-file", "HEAD@{1}"), ("fetch", "origin")])
def test_git_error_shows_a_plain_argument_as_written(args):
    assert _error(*args) == f"git {' '.join(args)}: fatal: unable to access"

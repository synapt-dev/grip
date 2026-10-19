"""A git or server message can echo a credential in a shape no pattern expects, so the values themselves are masked."""
from __future__ import annotations

import os
import subprocess

import pytest

from gr2.python_cli import gitops


def _repo(tmp_path, url):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "r", url], check=True)
    return repo


def _ssh(tmp_path, monkeypatch, body):
    script = tmp_path / "ssh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(script))


def _error(repo, *args):
    with pytest.raises(gitops.GitError) as caught:
        gitops.out(repo, *args)
    return str(caught.value)


# A real ssh server runs the command it is sent, in a directory with no such repository, and echoes the path.
SHELL_SERVER = 'for a; do l=$a; done\ncd "$(dirname "$0")/empty"\nexec sh -c "$l"\n'


@pytest.mark.parametrize("url", [
    "git@127.0.0.1:x.git?access_token=FAKEtokenA",           # (a) plain scp address
    'git@127.0.0.1:"x.git?access_token=FAKEtokenB"',         # (b) quoted scp address
    "ssh://git@127.0.0.1/x.git?access_token=FAKEtokenD",     # (d) ssh:// path echo
])
def test_a_token_the_server_echoes_with_its_path_is_masked(tmp_path, monkeypatch, url):
    (tmp_path / "empty").mkdir()
    _ssh(tmp_path, monkeypatch, SHELL_SERVER)
    repo = _repo(tmp_path, url)
    for args in (("ls-remote", "r"), ("ls-remote", url)):
        shown = _error(repo, *args)
        assert "FAKEtoken" not in shown and "does not appear" in shown


@pytest.mark.parametrize("url, echoed", [
    ("ssh://git@127.0.0.1/x.git?access_token=FAKEtoken123", "FAKEtoken123"),       # (c) the bare value
    ("ssh://git@127.0.0.1/x.git?access_token=%46AKEtoken45", "FAKEtoken45"),       # (e) encoded in the url
    ("ssh://git@127.0.0.1/x.git?private_token=FAKE/tok67", "FAKE%2Ftok67"),        # (e) encoded in the echo
])
def test_a_bare_token_in_stderr_is_masked(tmp_path, monkeypatch, url, echoed):
    _ssh(tmp_path, monkeypatch, f"echo 'denied for {echoed}' >&2\nexit 1\n")
    shown = _error(_repo(tmp_path, url), "ls-remote", "r")
    assert "tok" not in shown.replace("<a remote with a credential>", "") and "denied for" in shown


def test_a_url_without_a_credential_shows_unchanged(tmp_path):
    url = "https://127.0.0.1:1/plain.git"
    shown = _error(_repo(tmp_path, url), "ls-remote", url)
    assert url in shown


def test_a_word_containing_a_short_value_is_not_shredded(tmp_path, monkeypatch):
    _ssh(tmp_path, monkeypatch, "echo 'unable to grab the abc pack' >&2\nexit 1\n")
    shown = _error(_repo(tmp_path, "ssh://git@127.0.0.1/x.git?token=ab"), "ls-remote", "r")
    assert "unable to grab the abc pack" in shown


def test_credential_values_reads_args_and_remote_urls(tmp_path):
    repo = _repo(tmp_path, "https://user:FAKEpass1@h/x.git?token=FAKE%2Fv2")
    found = gitops.credential_values(["https://h/y.git?access_token=FAKEarg3"], repo)
    assert {"FAKEpass1", "FAKE%2Fv2", "FAKE/v2", "FAKEarg3", "user"} <= found
    assert not any(len(value) < 4 for value in found)


def test_a_hash_in_a_ref_name_is_not_taken_for_a_credential():
    args = ("ls-remote", "refs/heads/a#bcdef")
    shown = str(gitops.GitError(args, 128, "fatal: could not read refs/heads/a#bcdef", gitops.credential_values(args)))
    assert "refs/heads/a#bcdef" in shown and "<a remote with a credential>" not in shown


def test_a_url_fragment_is_still_masked():
    args = ("ls-remote", "https://h/x.git#secretfrag")
    secrets = gitops.credential_values(args)
    shown = str(gitops.GitError(args, 128, "fatal: secretfrag not found", secrets))
    assert "secretfrag" in secrets and "secretfrag" not in shown and "<a remote with a credential>" in shown


def test_a_hash_in_a_push_refspec_is_not_taken_for_a_credential():
    args = ("push", "https://h/x.git", "abc123:refs/heads/feat#1234")
    secrets = gitops.credential_values(args)
    shown = str(gitops.GitError(args, 1, "error: failed to push refs/heads/feat#1234", secrets))
    assert "feat#1234" in shown and "<a remote with a credential>" not in shown

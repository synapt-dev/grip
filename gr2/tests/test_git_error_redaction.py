"""A failed git command's error message names the command, never a credential carried in a remote URL.

GitError shows the command's first words, and for `ls-remote <remote>` the second word is the remote itself. git
strips userinfo from its own stderr; the argument echo has to do the same, or a refusal that quotes the error carries
the credential into a receipt."""
from __future__ import annotations

import dataclasses
import json
import subprocess

import pytest

from gr2.python_cli import declared_sets, gitops, merge_gate, next_steps
from gr2.python_cli.app import app
from tests.test_declared_sets import merge, record, world  # noqa: F401  (fixture and helpers)
from tests.test_review_merge_gate import _git_identity  # noqa: F401  (fixture)
from tests.test_review_merge_gate import check as gate_check, runner
from tests.test_review_merge_gate import world as gate_world  # noqa: F401  (fixture: one reviewed member)

TOKEN = "FAKETOKEN"
PLAIN = "https://127.0.0.1:9/a.git"
USERINFO = f"https://x:{TOKEN}@127.0.0.1:9/a.git"
QUERY = f"https://127.0.0.1:9/a.git?access_token={TOKEN}"
REAL_RUN = subprocess.run


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


def _cli_merge(w):
    res = runner.invoke(app, ["review", "merge", w["review"], "--into", "main", "--from", "feat", "--approvals", "0",
                              "--root", str(w["author"])])
    return res, json.loads(res.stdout.splitlines()[0]), res.stdout + res.stderr


def _refresh_with(monkeypatch, remote):
    """The merge lands, and the tracking refresh after it is handed `remote`, which no remote of the clone names."""
    real, seen = merge_gate._refresh_tracking, []
    monkeypatch.setattr(merge_gate, "_refresh_tracking", lambda repo, _, into: seen.append(repo) or real(repo, remote, into))
    return seen


@pytest.mark.parametrize("remote", [USERINFO, QUERY], ids=["userinfo", "query"])
def test_refresh_hint_never_shows_a_credential_remote(gate_world, monkeypatch, remote):
    gate_check(gate_world)
    _refresh_with(monkeypatch, remote)

    res, receipt, text = _cli_merge(gate_world)

    assert res.exit_code == 0 and receipt["tracking_stale"], text
    assert text.count(TOKEN) == 0, text


def test_refresh_hint_shows_a_plain_remote_as_before(gate_world, monkeypatch):
    gate_check(gate_world)
    seen = _refresh_with(monkeypatch, PLAIN)

    res, receipt, text = _cli_merge(gate_world)

    shown = next_steps._shown
    assert res.exit_code == 0, text
    assert [{k: v for k, v in s.items() if k != "member"} for s in receipt["tracking_stale"]] == [
        {"command": f"git -C {shown(seen[0])} fetch --no-tags --refmap= -- {shown(PLAIN)} "
                    f"{shown('+refs/heads/main:refs/remotes/origin/main')}",
         "reason": f"no remote of this clone names {PLAIN}"}], text


def _push_to(monkeypatch, w, remote):
    """The real `git push` of the merge runs against `remote` and fails; its stderr is kept for the row."""
    pushed = []

    def run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd[:1] == ["git"] and gitops.command_of(cmd) == "push":
            cmd = [remote if str(x) == str(w["remote"]) else x for x in cmd]
            kw["timeout"] = 20
            pushed.append(REAL_RUN(cmd, *a, **kw))
            return pushed[-1]
        return REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    return pushed


def test_push_error_never_shows_a_query_token(gate_world, monkeypatch):
    gate_check(gate_world)
    _push_to(monkeypatch, gate_world, QUERY)

    res, receipt, text = _cli_merge(gate_world)

    push_error = receipt["members"][0].get("push_error") or ""
    assert res.exit_code == 3 and "unable to access" in push_error, text
    assert text.count(TOKEN) == 0, text


def test_push_error_shows_a_plain_remote_as_before(gate_world, monkeypatch):
    gate_check(gate_world)
    pushed = _push_to(monkeypatch, gate_world, PLAIN)

    res, receipt, text = _cli_merge(gate_world)

    assert res.exit_code == 3 and pushed, text
    push_error = receipt["members"][0]["push_error"]
    assert "unable to access" in push_error and "https://127.0.0.1:9/a.git" in push_error, text


def _named_remote_error(tmp_path, url):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "r", url], check=True)
    with pytest.raises(gitops.GitError) as caught:
        gitops.out(repo, "ls-remote", "r")
    return caught.value


def test_git_error_never_shows_a_query_token_of_a_named_remote(tmp_path):
    exc = _named_remote_error(tmp_path, QUERY)

    assert "unable to access" in exc.stderr, exc.stderr
    assert TOKEN not in str(exc), str(exc)


def test_git_error_shows_a_plain_named_remote_as_before(tmp_path):
    exc = _named_remote_error(tmp_path, PLAIN)

    assert PLAIN in exc.stderr and str(exc) == f"git ls-remote r: {exc.stderr}", str(exc)

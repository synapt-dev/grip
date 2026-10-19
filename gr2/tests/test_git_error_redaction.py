"""A failed git command's error message names the command, never a credential carried in a remote URL.

GitError shows the command's first words, and for `ls-remote <remote>` the second word is the remote itself. git
strips userinfo from its own stderr; the argument echo has to do the same, or a refusal that quotes the error carries
the credential into a receipt."""
from __future__ import annotations

import dataclasses
import json
import subprocess
import urllib.parse

import pytest

from gr2.python_cli import declared_sets, gitops, grip, merge_gate, next_steps
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


@pytest.mark.parametrize("args, shown", [
    (("ls-remote", "https://127.0.0.1:9/a.git"), "ls-remote https://127.0.0.1:9/a.git"),
    (("ls-remote", "git@host:org/a.git"), "ls-remote git@host:org/a.git"),
    (("rev-parse", "refs/heads/a#b"), "rev-parse 'refs/heads/a#b'"),
    (("cat-file", "HEAD@{1}"), "cat-file 'HEAD@{1}'"),
    (("fetch", "origin"), "fetch origin"),
    (("rev-parse", "refs/heads/x"), "rev-parse refs/heads/x"),
])
def test_git_error_shows_a_plain_argument_as_a_shell_would_read_it(args, shown):
    assert _error(*args) == f"git {shown}: fatal: unable to access"


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


def test_git_error_never_shows_a_query_token_of_an_scp_address(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")  # git reaches an scp address over ssh; fail it offline
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(gitops.GitError) as caught:
        gitops.out(repo, "ls-remote", f"git@127.0.0.1:x.git?access_token={TOKEN}")

    assert caught.value.returncode != 0
    assert TOKEN not in str(caught.value), str(caught.value)


@pytest.mark.parametrize("args", [("push", "HEAD:refs/heads/x"), ("ls-remote", "git@127.0.0.1:x.git"),
                                  ("ls-remote", "./a:b")])
def test_git_error_shows_an_scp_address_and_a_colon_argument_as_written(args):
    assert _error(*args) == f"git {' '.join(args)}: fatal: unable to access"


def _rejected_scp_push(tmp_path, monkeypatch, address, named):
    """Push to `address` over a fake ssh that reaches a local bare repo whose pre-receive hook declines, so git prints
    the address (without its `user@`) in `To ...` and `failed to push some refs to '...'`."""
    repo, bare, ssh = tmp_path / "repo", tmp_path / "bare.git", tmp_path / "ssh"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    (bare / "hooks" / "pre-receive").write_text("#!/bin/sh\nexit 1\n")
    (bare / "hooks" / "pre-receive").chmod(0o755)
    ssh.write_text(f"#!/bin/sh\nexec git-receive-pack '{bare}'\n")
    ssh.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True)
    if named:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "r", address], check=True)
    with pytest.raises(gitops.GitError) as caught:
        gitops.out(repo, "push", "r" if named else address, "HEAD:refs/heads/x")
    assert "failed to push some refs to" in caught.value.stderr, caught.value.stderr
    return caught.value


def test_git_error_never_shows_a_token_of_a_named_scp_remote_in_git_stderr(tmp_path, monkeypatch):
    exc = _rejected_scp_push(tmp_path, monkeypatch, f"git@127.0.0.1:x.git?access_token={TOKEN}", named=True)

    assert TOKEN not in str(exc), str(exc)


def test_git_error_never_shows_a_token_of_an_scp_address_in_git_stderr(tmp_path, monkeypatch):
    exc = _rejected_scp_push(tmp_path, monkeypatch, f"git@127.0.0.1:x.git?access_token={TOKEN}", named=False)

    assert TOKEN not in str(exc), str(exc)


def test_git_error_shows_a_plain_scp_remote_in_git_stderr_as_before(tmp_path, monkeypatch):
    exc = _rejected_scp_push(tmp_path, monkeypatch, "git@127.0.0.1:x.git", named=True)

    assert str(exc) == f"git push r: {exc.stderr}".rstrip(": "), str(exc)
    assert "To 127.0.0.1:x.git\n" in str(exc) and "to '127.0.0.1:x.git'" in str(exc), str(exc)


@pytest.mark.parametrize("stderr", [
    "fatal: 'r' does not appear to be a git repository",
    "To 127.0.0.1:x.git\n ! [rejected]        HEAD -> x (fetch first)",
    "error: cannot push refs/heads/a:refs/heads/b to 'o'",
    "error: src refspec x does not match any",
])
def test_git_error_shows_git_stderr_with_a_colon_as_written(stderr):
    assert str(gitops.GitError(("push", "o"), 1, stderr)) == f"git push o: {stderr}"


@pytest.mark.parametrize("url, token", [
    ("ssh://us%3Aer%40x:FAKE%40TOK@127.0.0.1:9/x.git", "FAKE%40TOK"),
    ("https://user%3AFAKETOK@127.0.0.1:9/x.git", "FAKETOK"),
], ids=["ssh", "https"])
def test_a_percent_encoded_userinfo_is_a_credential(tmp_path, url, token):
    assert grip.url_has_credentials(url) is True
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(gitops.GitError) as caught:
        gitops.out(repo, "ls-remote", url)

    shown = str(caught.value)
    assert shown.startswith("git ls-remote "), shown
    assert token not in shown and urllib.parse.unquote(token) not in shown, shown

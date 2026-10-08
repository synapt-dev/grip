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

from tests.review_ref_helper import REVIEW_REF_PREFIX
from gr2.python_cli import grip
from gr2.python_cli.app import app
from gr2.python_cli import review_field_tree as fd


def _member_remote(root: Path, commit: str) -> str:
    """The bound remote of the one member, read from the field tree record the writer stores."""
    tree = subprocess.run(["git", "-C", str(root), "rev-parse", f"{commit}^{{tree}}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (member,) = fd.read_record(root, tree)["members"]
    return member["remote"]

runner = CliRunner()
PREFIX = REVIEW_REF_PREFIX
TOKEN = "FAKE-TOKEN-0000"
CRED = f"https://user:{TOKEN}@example.invalid/o/member.git"
FOREIGN = "https://example.invalid/o/destination.git"
PORTABLE = "https://example.invalid/o/member.git"


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
    gitconfig.write_text(f'[url "file://{remote}"]\n\tinsteadOf = {CRED}\n\tinsteadOf = {PORTABLE}\n'
                         f'[url "file://{dest}"]\n\tinsteadOf = {FOREIGN}\n')
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
    git(author, "config", "user.name", "Fixture")
    git(author, "config", "user.email", "fixture@example.invalid")
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
    assert TOKEN in _member_remote(world["author"], commit)
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


@pytest.mark.parametrize("spelling", ["relative", "dot-relative", "file-url"])
def test_publish_keeps_every_host_local_spelling_on_the_host(world, monkeypatch, spelling):
    # Found in review: `../remote.git` is not absolute, so an absolute-path test let it publish off-host.
    remote = {"relative": "../remote.git", "dot-relative": "./../remote.git", "file-url": f"file://{world['remote']}"}[spelling]
    made = bind(world, monkeypatch, remote)
    assert made.exit_code == 0, made.output
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    assert _member_remote(world["author"], commit) == remote
    off_host = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", FOREIGN)
    assert off_host.exit_code != 0, off_host.output
    assert "local_path_remote" in off_host.output, off_host.output
    assert git(world["dest"], "for-each-ref") == "", "nothing may be pushed before the refusal"
    assert reviews(world["author"]).rsplit("/", 1)[1] == commit, "the author's bind is unchanged"
    on_host = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", world["dest"])
    assert on_host.exit_code == 0, on_host.output


def test_publish_sends_a_portable_record_off_host(world, monkeypatch):
    # Matched positive: the refusal is about the record's remote, not about https destinations.
    made = bind(world, monkeypatch, PORTABLE)
    assert made.exit_code == 0, made.output
    commit = reviews(world["author"]).rsplit("/", 1)[1]
    sent = review(world["author"], monkeypatch, "publish", "gr:" + commit, "--remote", FOREIGN)
    assert sent.exit_code == 0, sent.output
    assert git(world["dest"], "rev-parse", PREFIX + commit) == commit


@pytest.mark.parametrize("remote,portable", [
    ("https://github.com/o/r.git", True), ("http://host/o/r.git", True), ("ssh://git@host/o/r.git", True),
    ("git://host/o/r.git", True), ("git@github.com:o/r.git", True), ("host.example:o/r.git", True),
    ("/abs/r.git", False), ("../r.git", False), ("./r.git", False), ("r.git", False), ("origin", False),
    ("file:///abs/r.git", False), ("ext::sh -c x", False), ("C:/repos/r.git", False), ("", False),
])
def test_portable_remote_classifier(remote, portable):
    assert grip._is_portable_remote(remote) is portable


# --- A remote that git would parse as an OPTION must never reach a git argv (1.052123) -------------------------
#
# `git ls-remote --upload-pack=<cmd> ...` runs <cmd>. `review bind` used to hand its remote to `ls-remote` before
# validating it, so `--remote '--upload-pack=touch S'` ran the command and only then refused (remote_unreadable).
# Every row watches a SENTINEL file the command would create; a refusal that still created it is the defect.

def option_shaped(sentinel):
    return f"--upload-pack=touch {sentinel}"


def test_instrument_an_option_shaped_remote_does_run_when_nothing_guards_it(world, tmp_path):
    # Control: with no guard and no `--`, git really executes the command. Without this a green row below proves nothing.
    sentinel = tmp_path / "RAN_CONTROL"
    subprocess.run(["git", "ls-remote", option_shaped(sentinel), "refs/heads/main"], cwd=world["author"],
                   capture_output=True, text=True)
    assert sentinel.exists(), "the sentinel must be creatable, or every no-execution row below is vacuous"


def test_bind_refuses_an_option_shaped_remote_given_by_flag_before_git_runs(world, monkeypatch, tmp_path):
    sentinel = tmp_path / "RAN_FLAG"
    result = bind(world, monkeypatch, option_shaped(sentinel))
    assert result.exit_code != 0, result.output
    assert not sentinel.exists(), "bind ran the remote as git's --upload-pack before it validated it"
    assert "invalid_field" in result.output, result.output
    assert reviews(world["author"]) == ""


def test_bind_refuses_an_option_shaped_remote_from_the_manifest_before_git_runs(world, monkeypatch, tmp_path):
    sentinel = tmp_path / "RAN_MANIFEST"
    toml = world["author"] / "grip.toml"
    toml.write_text(toml.read_text().replace(str(world["remote"]), option_shaped(sentinel)))
    assert "--upload-pack" in toml.read_text()
    result = review(world["author"], monkeypatch, "bind")
    assert result.exit_code != 0, result.output
    assert not sentinel.exists(), "a manifest remote reached `git ls-remote` as an option"
    assert "invalid_field" in result.output, result.output
    assert reviews(world["author"]) == ""


def test_bind_refuses_a_remote_with_a_control_character(world, monkeypatch):
    result = bind(world, monkeypatch, str(world["remote"]) + "\x01")
    assert result.exit_code != 0, result.output
    assert "invalid_field" in result.output, result.output
    assert reviews(world["author"]) == ""


def test_remote_head_never_runs_an_option_shaped_remote(world, tmp_path):
    sentinel = tmp_path / "RAN_REMOTE_HEAD"
    with pytest.raises(Exception):
        grip._remote_head(world["author"], option_shaped(sentinel), "refs/heads/main")
    assert not sentinel.exists(), "_remote_head passes its remote to ls-remote without `--`"


def test_head_present_on_remote_never_runs_an_option_shaped_remote(world, tmp_path):
    sentinel = tmp_path / "RAN_HEAD_PRESENT"
    # This sink passes the remote as the ONLY ls-remote argument, so git resolves a default remote when it parses the
    # value as an option. Give the workspace root an origin, or git fails before it runs anything and the row is vacuous.
    git(world["author"], "remote", "add", "origin", world["remote"])
    with pytest.raises(Exception):
        grip._head_present_on_remote(world["author"], option_shaped(sentinel), world["head"])
    assert not sentinel.exists(), "_head_present_on_remote passes its remote to ls-remote without `--`"


def test_a_dash_leading_ref_is_not_an_injection_and_bind_still_refuses(world, monkeypatch, tmp_path):
    # Control for the scope: git takes everything after the repository as a pattern, so a ref is not the sink.
    sentinel = tmp_path / "RAN_REF"
    result = review(world["author"], monkeypatch, "bind", "--repo", "member", "--remote", world["remote"],
                    "--base", world["base"], "--head", world["head"], "--ref", option_shaped(sentinel),
                    "--source", world["member"])
    assert result.exit_code != 0, result.output
    assert not sentinel.exists()
    assert reviews(world["author"]) == ""

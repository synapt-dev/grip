"""The pr verbs take the current review as their subject, as `review show` does.

The stranger's sequence measured on grip dev c3ab3ac7: after `review bind` and `review show` pass with no
arguments, `pr create` / `pr status` / `pr merge` with no arguments demanded WORKSPACE_ROOT, then OWNER_UNIT,
then refused "no current lane recorded for unit". A review knows its members, heads, remotes and branches, so
the pr verbs resolve from it: explicit inputs win, omitted ones come from the review, and missing or ambiguous
context names the one input needed. Fake adapter, no host; every fake https remote is rewritten to a local bare.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from gr2.python_cli import app as app_mod
from gr2.python_cli import pr as pr_ops
import typer
from gr2.python_cli.app import app

from tests.test_pr_events import FakeAdapter

runner = CliRunner()
KEYS = ("alpha", "beta")
URL = {k: f"https://github.com/o/{k}.git" for k in KEYS}


def git(root, *args):
    p = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def gr2(root, monkeypatch, *args):
    monkeypatch.chdir(root)
    return runner.invoke(app, list(map(str, args)))


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    # The rewrite lives ONLY in the workspace repo's config (where bind dials), never globally: a global
    # insteadOf also rewrites what `git remote get-url` returns, so store init would record the local path
    # as each member's remote and the pr verbs would never see a real host slug.
    isolated_config = tmp_path / "identity-gitconfig"
    isolated_config.write_text("[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for var in ("GR2_ACTOR", "SYNAPT_AGENT_ID", "SYNAPT_AGENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    author = tmp_path / "author"
    author.mkdir()
    heads = {}
    for k in KEYS:
        seed = tmp_path / (k + "-seed")
        seed.mkdir()
        git(tmp_path, "init", "--bare", "-b", "main", tmp_path / (k + ".git"))
        git(seed, "init", "-b", "main")
        (seed / "p.txt").write_text(k + "\n")
        git(seed, "add", "p.txt")
        git(seed, "commit", "-m", "base")
        git(seed, "push", tmp_path / (k + ".git"), "main")
        git(author, "clone", "--branch", "main", tmp_path / (k + ".git"), k)
        git(author / k, "remote", "set-url", "origin", URL[k])
    init = gr2(author, monkeypatch, "store", "init", author)
    assert init.exit_code == 0, init.output
    for k in KEYS:
        assert URL[k] in (author / "grip.toml").read_text(), "the manifest must carry the real remote"
        git(author, "config", "--local", f"url.file://{tmp_path / (k + '.git')}.insteadOf", URL[k])
    for k in KEYS:
        git(author / k, "switch", "-c", "feat/two-member")
        (author / k / "p.txt").write_text(k + " reviewed\n")
        git(author / k, "commit", "-am", "change")
        heads[k] = git(author / k, "rev-parse", "HEAD")
    bound = gr2(author, monkeypatch, "review", "bind")
    assert bound.exit_code == 0, bound.output
    for k in KEYS:  # the reviewed branch is pushed after the bind, as an author does before opening PRs
        git(author / k, "push", tmp_path / (k + ".git"), "feat/two-member")
    shown = gr2(author, monkeypatch, "review", "show", "--json")
    assert shown.exit_code == 0, shown.output
    target = json.loads(shown.stdout)["id"]
    adapter = FakeAdapter()
    monkeypatch.setattr(app_mod.platform_ops, "get_platform_adapter", lambda name: adapter)
    # THE INTERCEPTION, declared: the fixture's own insteadOf sends each fake github.com remote to a local
    # bare, and this seam reports it as the github.com remote, which is what the test means it to stand for.
    # The decoy test below removes the seam and expects the rewrite to be refused.
    real_effective = app_mod._effective_remote
    monkeypatch.setattr(app_mod, "_effective_remote", lambda ws, remote: remote)
    repos = {k: f"o/{k}" for k in KEYS}  # the owner/repo slug the platform adapter receives
    return dict(author=author, heads=heads, target=target, adapter=adapter, repos=repos, bare={k: tmp_path / (k + ".git") for k in KEYS},
                real_effective=real_effective, tmp=tmp_path)


def test_instrument_rewrites_fake_remotes_locally(reviewed):
    assert "refs/heads/main" in git(reviewed["author"], "ls-remote", URL["alpha"])


def test_review_creation_refuses_credential_remote_before_adapter_or_state(reviewed, monkeypatch):
    remote = "https://user:fixture-private-token@github.com/o/alpha.git"
    # Inject a credentialed resolved subject at the reader seam, bypassing bind's
    # existing writer refusal to exercise PR creation's own host validation.
    monkeypatch.setattr(app_mod, "resolve_review_subject", lambda *args: (
        reviewed["target"], [dict(key="alpha", remote=remote, path="alpha",
                                  commit=reviewed["heads"]["alpha"])],
    ))
    result = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert result.exit_code == 2, result.output
    assert "alpha" in result.output
    assert "fixture-private-token" not in result.output
    assert remote not in result.output
    assert reviewed["adapter"].created == []
    assert not (reviewed["author"] / ".grip/pr_groups").exists()


def test_bare_pr_create_opens_one_group_for_the_review(reviewed, monkeypatch):
    made = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert made.exit_code == 0, made.output
    created = reviewed["adapter"].created
    assert sorted(r.repo for r in created) == sorted(reviewed["repos"].values())
    assert {r.head_branch for r in created} == {"feat/two-member"}
    assert {r.repo: r.remote for r in created} == {reviewed["repos"][k]: URL[k] for k in KEYS}
    groups = [json.loads(p.read_text()) for p in (reviewed["author"] / ".grip" / "pr_groups").glob("*.json")]
    assert len(groups) == 1 and groups[0]["review_target"] == reviewed["target"]
    assert {p["repo"]: p["remote"] for p in groups[0]["prs"]} == {
        reviewed["repos"][k]: URL[k] for k in KEYS
    }


def test_bare_pr_status_reads_the_review_group(reviewed, monkeypatch):
    assert gr2(reviewed["author"], monkeypatch, "pr", "create", "--json").exit_code == 0
    status = gr2(reviewed["author"], monkeypatch, "pr", "status", "--json")
    assert status.exit_code == 0, status.output


def test_bare_pr_merge_pins_the_reviewed_heads(reviewed, monkeypatch):
    assert gr2(reviewed["author"], monkeypatch, "pr", "create", "--json").exit_code == 0
    seen = {}

    def merge_pr_group(**kwargs):
        seen.update(kwargs)
        return {"pr_group_id": kwargs["pr_group_id"], "group_state": "merged", "completed": []}

    monkeypatch.setattr(pr_ops, "merge_pr_group", merge_pr_group)
    merged = gr2(reviewed["author"], monkeypatch, "pr", "merge", "--json")
    assert merged.exit_code == 0, merged.output
    assert seen["expected_heads"] == {reviewed["repos"][k]: reviewed["heads"][k] for k in KEYS}


def test_create_refuses_a_member_whose_checkout_moved_after_the_review(reviewed, monkeypatch):
    member = reviewed["author"] / "alpha"
    (member / "p.txt").write_text("unreviewed\n")
    git(member, "commit", "-am", "after review")
    made = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert made.exit_code != 0
    assert "alpha" in made.output
    assert reviewed["adapter"].created == [], "no PR may open for unreviewed bytes"


def test_legacy_unit_lane_form_is_unchanged(reviewed, monkeypatch):
    legacy = gr2(reviewed["author"], monkeypatch, "pr", "status", reviewed["author"], "nobody")
    assert legacy.exit_code == 1 and "no current lane recorded for unit: nobody" in legacy.output


def refused(result, *words):
    assert result.exit_code != 0, result.output
    for word in words:
        assert word in result.output, result.output


def test_create_refuses_when_the_remote_branch_is_not_the_reviewed_commit(reviewed, monkeypatch):
    # The PR carries the REMOTE branch, so a checkout at the reviewed commit is not enough.
    older = git(reviewed["bare"]["alpha"], "rev-parse", "main")
    git(reviewed["bare"]["alpha"], "update-ref", "refs/heads/feat/two-member", older)
    refused(gr2(reviewed["author"], monkeypatch, "pr", "create", "--json"), "alpha", "not the reviewed commit")
    assert reviewed["adapter"].created == []


def test_create_refuses_a_branch_that_was_never_pushed(reviewed, monkeypatch):
    git(reviewed["bare"]["beta"], "update-ref", "-d", "refs/heads/feat/two-member")
    refused(gr2(reviewed["author"], monkeypatch, "pr", "create", "--json"), "beta", "absent")
    assert reviewed["adapter"].created == []


def test_a_second_create_for_one_review_is_refused(reviewed, monkeypatch):
    assert gr2(reviewed["author"], monkeypatch, "pr", "create", "--json").exit_code == 0
    refused(gr2(reviewed["author"], monkeypatch, "pr", "create", "--json"), "already exists")
    assert len(reviewed["adapter"].created) == 2
    assert gr2(reviewed["author"], monkeypatch, "pr", "status", "--json").exit_code == 0


@pytest.mark.parametrize("verb", ["create", "status", "merge"])
def test_review_beside_a_legacy_unit_is_refused(reviewed, monkeypatch, verb):
    refused(gr2(reviewed["author"], monkeypatch, "pr", verb, reviewed["author"], "unit", "--review", reviewed["target"]),
            "give one or the other")


def test_merge_reads_a_configured_method_when_the_root_has_one(reviewed, monkeypatch):
    assert gr2(reviewed["author"], monkeypatch, "pr", "create", "--json").exit_code == 0
    spec = reviewed["author"] / ".grip" / "workspace_spec.toml"
    spec.write_text('schema_version = 1\nworkspace_name = "author"\n[settings]\nmerge_method = "squash"\n')
    seen = {}
    monkeypatch.setattr(pr_ops, "merge_pr_group", lambda **kw: seen.update(kw) or {"pr_group_id": kw["pr_group_id"], "group_state": "merged", "completed": []})
    assert gr2(reviewed["author"], monkeypatch, "pr", "merge", "--json").exit_code == 0
    assert "squash" in str(seen["method"]).lower()


@pytest.mark.parametrize("remote,path,why", [
    ("https://gitlab.com/o/alpha.git", "alpha", "names no GitHub owner/repo"),
    ("https://github.com/o/alpha.git?access_token=FAKE-TOKEN-0000", "alpha", "names no GitHub owner/repo"),
    ("https://github.com/o/alpha#frag", "alpha", "names no GitHub owner/repo"),
    ("https://github.com/o/alpha with space", "alpha", "names no GitHub owner/repo"),
    ("https://github.com/o/alpha.git", "../outside", "leaves the workspace"),
    ("https://github.com/o/alpha.git", "gone", "is missing"),
])
def test_members_that_cannot_be_addressed_are_refused_by_name(tmp_path, remote, path, why, capsys):
    (tmp_path / "alpha").mkdir()
    member = dict(key="alpha", remote=remote, path=path, commit="0" * 40, base="0" * 40)
    with pytest.raises(typer.Exit):
        app_mod._review_members_on_host(tmp_path.resolve(), [member])
    assert why in capsys.readouterr().err


def test_two_members_on_one_repo_are_refused(tmp_path, capsys):
    for k in KEYS:
        (tmp_path / k).mkdir()
    members = [dict(key=k, remote="git@github.com:o/mono.git", path=k, commit="0" * 40, base="0" * 40) for k in KEYS]
    with pytest.raises(typer.Exit):
        app_mod._review_members_on_host(tmp_path.resolve(), members)
    assert "o/mono" in capsys.readouterr().err


def test_an_ssh_url_remote_addresses_its_repo(tmp_path):
    (tmp_path / "alpha").mkdir()
    member = dict(key="alpha", remote="ssh://git@github.com/o/alpha.git", path="alpha", commit="0" * 40, base="0" * 40)
    assert list(app_mod._review_members_on_host(tmp_path.resolve(), [member])) == ["o/alpha"]


@pytest.mark.parametrize("verb", ["create", "status", "merge"])
@pytest.mark.parametrize("option", ["--title", "--base", "--review"])
def test_a_trailing_option_without_its_value_is_a_usage_error(reviewed, monkeypatch, verb, option):
    opts = {o for p in typer.main.get_command(app).commands["pr"].commands[verb].params for o in p.opts}
    if option not in opts:
        pytest.skip(f"pr {verb} has no {option}")
    result = gr2(reviewed["author"], monkeypatch, "pr", verb, option)
    assert result.exit_code == 2, result.output
    assert "requires an argument" in result.output, result.output
    assert reviewed["adapter"].created == []


def test_a_rewrite_to_a_decoy_holding_the_reviewed_commit_is_refused(reviewed, monkeypatch):
    # The recorded github.com branch is stale, and a url rewrite points the dial at a decoy whose branch
    # IS the reviewed commit. The tip read would pass while the PR opens on the stale branch.
    older = git(reviewed["bare"]["alpha"], "rev-parse", "main")
    decoy = reviewed["tmp"] / "decoy.git"
    git(reviewed["tmp"], "clone", "--bare", "-q", reviewed["bare"]["alpha"], decoy)
    git(reviewed["bare"]["alpha"], "update-ref", "refs/heads/feat/two-member", older)
    git(reviewed["author"], "config", "--local", "--unset-all", f"url.file://{reviewed['bare']['alpha']}.insteadOf")
    git(reviewed["author"], "config", "--local", f"url.file://{decoy}.insteadOf", URL["alpha"])
    assert git(reviewed["author"], "ls-remote", URL["alpha"], "refs/heads/feat/two-member").split()[0] == reviewed["heads"]["alpha"]
    monkeypatch.setattr(app_mod, "_effective_remote", reviewed["real_effective"])
    refused(gr2(reviewed["author"], monkeypatch, "pr", "create", "--json"), "alpha", "url rewrite")
    assert reviewed["adapter"].created == []


def test_a_rewrite_that_keeps_the_github_repo_is_the_same_endpoint(tmp_path):
    # https -> ssh for the same repository is a common user rewrite; it names the same repo.
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "--local", "url.git@github.com:.insteadOf", "https://github.com/")
    dialled = app_mod._effective_remote(tmp_path, URL["alpha"])
    assert dialled == "git@github.com:o/alpha.git"
    assert app_mod._github_slug(dialled) == app_mod._github_slug(URL["alpha"]) == "o/alpha"


def test_a_remote_that_never_answers_is_refused_within_the_bound(reviewed, monkeypatch):
    # A peer that accepts the connection and never replies: without a bound, pr create waits forever.
    import socket, threading, time
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    server.settimeout(0.1)
    held = []
    stop = threading.Event()

    def accept_silent_peers():
        while not stop.is_set():
            try:
                peer = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            held.append(peer)

    listener = threading.Thread(target=accept_silent_peers)
    listener.start()
    port = server.getsockname()[1]
    try:
        git(reviewed["author"], "config", "--local", "--unset-all", f"url.file://{reviewed['bare']['alpha']}.insteadOf")
        git(reviewed["author"], "config", "--local", f"url.http://127.0.0.1:{port}/alpha.git.insteadOf", URL["alpha"])
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)  # dial the silent peer itself, not the dead proxy
        monkeypatch.setattr(app_mod, "_REMOTE_TIP_TIMEOUT", 2.0)
        started = time.monotonic()
        result = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
        assert time.monotonic() - started < 20
        refused(result, "alpha", "timed out")
        assert reviewed["adapter"].created == []
    finally:
        stop.set()
        server.close()
        listener.join(timeout=2)
        for connection, _ in held:
            connection.close()
    assert not listener.is_alive(), "silent peer fixture listener did not stop"

"""A bare `gr2 review publish <rid>` publishes every member: each pinned head to its own recorded remote, then the
review ref there, or refuses before anything leaves the host.

Rows P0/A1..A9, J (stdout is the JSON receipt with no flag) and S (cwd is a subdirectory of the workspace). Real git,
bare local remotes under tmp_path, no network. Exit codes match `review merge`: 0 all published, 2 refused in
preflight, 3 a later failure that landed nothing, 4 partial.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX

runner = CliRunner()


def git(root, *args, check=True):
    p = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *map(str, args)],
                       cwd=root, capture_output=True, text=True)
    if check:
        assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def tip(remote, ref):
    """What the remote serves at exactly `ref`, or None (not ls-remote's first token: a lookalike by tail sorts first)."""
    out = git(remote, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out or None


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "Fixture"), ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
                 ("GIT_COMMITTER_NAME", "Fixture"), ("GIT_COMMITTER_EMAIL", "fixture@example.invalid")):
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def build(tmp_path, monkeypatch, *, shared=False, visibility=None, names=("alpha", "beta")):
    """Two members, alpha and beta, pinned into ONE review; nothing pushed, nothing published.
    shared=True puts both on one bare remote (alpha's), on branches feat-alpha and feat-beta.
    visibility=None declares every member private; a member a given dict does not name binds undeclared (no field)."""
    if visibility is None:
        visibility = {name: "private" for name in names}
    author = tmp_path / "author"
    author.mkdir()
    w = {"author": author, "members": {}}
    for name in names:
        remote = tmp_path / ("alpha.git" if shared else f"{name}.git")
        if not remote.exists():
            git(tmp_path, "init", "--bare", "-b", "main", remote)
            seed = tmp_path / f"seed-{name}"
            seed.mkdir()
            git(seed, "init", "-b", "main")
            (seed / "payload.txt").write_text(f"{name} base\n")
            git(seed, "add", "payload.txt")
            git(seed, "commit", "-m", "base")
            git(seed, "push", remote, "main")
        git(author, "clone", "--no-local", "--branch", "main", remote, name)
        w["members"][name] = {"remote": remote, "base": git(author / name, "rev-parse", "HEAD"), "repo": author / name,
                              "branch": f"feat-{name}" if shared else "feat"}
    assert runner.invoke(app, ["store", "init", str(author)]).exit_code == 0
    rows = []
    for name, m in w["members"].items():
        git(m["repo"], "checkout", "-b", m["branch"])
        (m["repo"] / "payload.txt").write_text(f"{name} reviewed\n")
        git(m["repo"], "add", "payload.txt")
        git(m["repo"], "commit", "-m", "reviewed")
        m["head"] = git(m["repo"], "rev-parse", "HEAD")
        rows.append({"key": name, "path": name, "remote": str(m["remote"]), "base": m["base"], "head": m["head"],
                     "ref": "refs/heads/main", "source": str(m["repo"])})
        if name in visibility:
            rows[-1]["visibility"] = visibility[name]
    (tmp_path / "rows.json").write_text(json.dumps(rows))
    monkeypatch.chdir(author)
    made = runner.invoke(app, ["review", "bind", "--rows-json", str(tmp_path / "rows.json")])
    assert made.exit_code == 0, made.output
    w["review"] = "gr:" + git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).rsplit("/", 1)[1]
    w["full"] = w["review"][3:]
    return w


@pytest.fixture
def w(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch)


def publish(*extra):
    return runner.invoke(app, ["review", "publish", *extra])


def receipt(out):
    return json.loads(out.stdout)


def review_on(remote, full):
    out = git(remote, "for-each-ref", "--format=%(objectname)", REVIEW_REF_PREFIX + full)
    return out or None


def untouched(m):
    """The member's remote carries neither the feature head nor any review ref."""
    return tip(m["remote"], f"refs/heads/{m['branch']}") is None and not git(m["remote"], "for-each-ref", REVIEW_REF_PREFIX)


def test_p0_a1_one_bare_call_publishes_every_member_and_a_second_workspace_receives_both(w, tmp_path):
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    for m in w["members"].values():
        assert tip(m["remote"], "refs/heads/feat") == m["head"]
        assert review_on(m["remote"], w["full"]) == w["full"]
    other = tmp_path / "reviewer"
    other.mkdir()
    for name, m in w["members"].items():
        git(other, "clone", "--no-local", "--single-branch", "--branch", "main", m["remote"], name)
    assert runner.invoke(app, ["store", "init", str(other)]).exit_code == 0
    for name, m in w["members"].items():
        got = runner.invoke(app, ["review", "receive", str(other), w["review"], "--remote", str(m["remote"])])
        assert got.exit_code == 0, got.output
        heads = {h["key"]: h["status"] for h in json.loads(got.stdout)["heads"]}
        assert heads[name] == "fetched", heads


def test_a2_remote_form_is_unchanged_and_touches_only_that_remote(w):
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    out = publish(w["review"], "--remote", str(alpha["remote"]))
    assert out.exit_code == 0, out.output
    assert set(receipt(out)) == {"id", "ref", "remote"}
    assert tip(alpha["remote"], "refs/heads/feat") is None, "--remote publishes the review only, never a head"
    assert review_on(alpha["remote"], w["full"]) == w["full"]
    assert untouched(beta)


def test_a3_a_commit_after_the_pin_refuses_and_nothing_leaves(w):
    beta = w["members"]["beta"]
    (beta["repo"] / "later.txt").write_text("after the pin\n")
    git(beta["repo"], "add", "later.txt")
    git(beta["repo"], "commit", "-m", "after the pin")
    out = publish(w["review"])
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("head_moved_since_pin: beta"), out.stdout
    assert all(untouched(m) for m in w["members"].values())


def _mid_merge(m):
    git(m["repo"], "branch", "other", m["base"])
    git(m["repo"], "checkout", "-q", "other")
    (m["repo"] / "payload.txt").write_text("conflicting\n")
    git(m["repo"], "commit", "-qam", "conflicting")
    git(m["repo"], "checkout", "-q", m["branch"])
    git(m["repo"], "merge", "other", check=False)
    assert git(m["repo"], "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
    assert git(m["repo"], "rev-parse", "HEAD") == m["head"]


@pytest.mark.parametrize("form", ["bare", "remote"])
def test_a4_a_member_mid_merge_refuses_in_both_forms(w, form):
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    _mid_merge(beta)
    out = publish(w["review"]) if form == "bare" else publish(w["review"], "--remote", str(alpha["remote"]))
    assert out.exit_code == 2, out.output
    assert "unresolved_merge" in out.output and "beta" in out.output, out.output
    assert all(untouched(m) for m in w["members"].values())


@pytest.mark.parametrize("form", ["bare", "remote"])
def test_a4b_the_mid_merge_refusal_names_the_paths_and_says_what_to_do(w, form):
    _mid_merge(w["members"]["beta"])
    out = publish(w["review"]) if form == "bare" else publish(w["review"], "--remote", str(w["members"]["alpha"]["remote"]))
    assert "refused: unresolved_merge: beta: payload.txt; resolve or abort the merge in this clone" in out.stderr, out.stderr
    assert "expected" not in out.stderr, out.stderr


def _reject_pushes(m):
    hook = m["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'fixture: rejected' >&2\nexit 1\n")
    hook.chmod(0o755)
    return hook


def test_a5_a_later_member_failing_is_partial_exit_4_with_a_row_per_member(w):
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    _reject_pushes(beta)
    out = publish(w["review"])
    assert out.exit_code == 4, out.output
    r = receipt(out)
    assert r["complete"] is False
    rows = {m["key"]: m for m in r["members"]}
    assert rows["alpha"]["state"] == "published" and rows["beta"]["state"] == "failed", r
    assert rows["beta"]["error"], r
    assert tip(alpha["remote"], "refs/heads/feat") == alpha["head"]
    assert review_on(alpha["remote"], w["full"]) == w["full"]
    assert untouched(beta)
    assert r["next"] and "gr2 review publish" in r["next"]


def test_a5c_the_partial_next_line_runs_as_printed_from_a_path_with_a_space(tmp_path, monkeypatch):
    """The receipt's `next` is a command a person pastes: a workspace path holding a space must come back as ONE word."""
    base = tmp_path / "work space"
    base.mkdir()
    w = build(base, monkeypatch)
    _reject_pushes(w["members"]["beta"])
    out = publish(w["review"])
    assert out.exit_code == 4, out.output
    line = receipt(out)["next"].split("   #", 1)[0]
    argv = shlex.split(line)
    assert argv[:3] == ["gr2", "review", "publish"] and argv[4] == w["review"], argv
    assert Path(argv[3]).resolve() == w["author"].resolve(), argv

def test_a5b_the_first_member_failing_landed_nothing_exit_3(w):
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    _reject_pushes(alpha)
    out = publish(w["review"])
    assert out.exit_code == 3, out.output
    rows = {m["key"]: m["state"] for m in receipt(out)["members"]}
    assert rows == {"alpha": "failed", "beta": "not_attempted"}, rows
    assert all(untouched(m) for m in w["members"].values())


def test_a6_a_rerun_after_partial_resumes_and_is_idempotent(w):
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    hook = _reject_pushes(beta)
    assert publish(w["review"]).exit_code == 4
    alpha_tip = tip(alpha["remote"], "refs/heads/feat")
    hook.unlink()
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    rows = {m["key"]: m["state"] for m in receipt(out)["members"]}
    assert rows == {"alpha": "already_published", "beta": "published"}, rows
    assert tip(alpha["remote"], "refs/heads/feat") == alpha_tip
    assert tip(beta["remote"], "refs/heads/feat") == beta["head"]


def test_a7_a_remote_feature_ahead_of_the_pin_refuses_before_any_push(w, tmp_path):
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    other = tmp_path / "someone"
    git(tmp_path, "clone", "--no-local", beta["repo"], other)
    git(other, "checkout", "-q", "feat")
    (other / "theirs.txt").write_text("someone else\n")
    git(other, "add", "theirs.txt")
    git(other, "commit", "-m", "theirs")
    git(other, "push", beta["remote"], "feat")
    ahead = tip(beta["remote"], "refs/heads/feat")
    out = publish(w["review"])
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("feature_moved: beta"), out.stdout
    assert tip(beta["remote"], "refs/heads/feat") == ahead
    assert untouched(alpha)


def test_a8_two_members_on_one_remote_publish_the_review_once(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, shared=True)
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    r = receipt(out)
    assert len(r["remotes"]) == 1, r["remotes"]
    remote = w["members"]["alpha"]["remote"]
    for m in w["members"].values():
        assert tip(remote, f"refs/heads/{m['branch']}") == m["head"]
    assert review_on(remote, w["full"]) == w["full"]


def test_a9_a_detached_member_refuses_no_branch_and_nothing_leaves(w):
    beta = w["members"]["beta"]
    git(beta["repo"], "checkout", "-q", "--detach", beta["head"])
    out = publish(w["review"])
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("no_branch: beta"), out.stdout
    assert all(tip(m["remote"], "refs/heads/main") == m["base"] and untouched(m) for m in w["members"].values())


def test_j_the_bare_form_prints_its_json_receipt_on_stdout_with_no_flag(w):
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    r = json.loads(out.stdout)
    assert r["id"] == w["review"] and r["complete"] is True and r["exit"] == 0
    assert [m["key"] for m in r["members"]] == ["alpha", "beta"]
    assert "published: alpha" in out.stderr


def test_s_from_a_subdirectory_of_the_workspace(w, monkeypatch):
    sub = w["author"] / "alpha" / "docs"
    sub.mkdir()
    monkeypatch.chdir(sub)
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    for m in w["members"].values():
        assert tip(m["remote"], "refs/heads/feat") == m["head"]
        assert review_on(m["remote"], w["full"]) == w["full"]


# ls-remote answers refs/heads/<branch> by tail, so refs/heads/a/refs/heads/<branch> answers too and sorts first; the
# first token of the listing is then the lookalike's tip. Both reads of the member's branch (before the push and after
# it) take the row named exactly the branch, and a malformed line is a fault, not absence.

def _lookalike(m, oid):
    git(m["remote"], "update-ref", f"refs/heads/a/refs/heads/{m['branch']}", oid)


def _listing(monkeypatch, rewrite, call):
    """Hand the `call`-th ls-remote of beta's branch (1 = before the push, 2 = after) through `rewrite`."""
    from gr2.python_cli import grip
    real, seen = grip.git, {"n": 0}

    def fake(cwd, *args, **kw):
        out = real(cwd, *args, **kw)
        if args[:1] == ("ls-remote",) and args[-1] == "refs/heads/feat" and "beta" in args[-2]:
            seen["n"] += 1
            if seen["n"] == call:
                return subprocess.CompletedProcess(out.args, out.returncode, rewrite(out.stdout), out.stderr)
        return out
    monkeypatch.setattr(grip, "git", fake)


def test_l1_before_the_push_a_lookalike_sorting_first_is_not_read_as_the_feature(w, tmp_path):
    beta = w["members"]["beta"]
    stray = git(beta["repo"], "commit-tree", "-m", "stray", git(beta["repo"], "rev-parse", "HEAD^{tree}"))
    git(beta["repo"], "push", beta["remote"], f"{stray}:refs/heads/keep")  # an object the remote holds, off the feature
    _lookalike(beta, stray)
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    assert tip(beta["remote"], "refs/heads/feat") == beta["head"]


def test_l2_before_the_push_a_malformed_line_refuses_and_nothing_leaves(w, monkeypatch):
    beta = w["members"]["beta"]
    _listing(monkeypatch, lambda s: f"{beta['head']}\trefs/heads/feat\nnot a row\n", 1)
    out = publish(w["review"])
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("remote_unreadable: beta"), out.stdout
    assert untouched(beta) and untouched(w["members"]["alpha"])


def test_l3_after_the_push_a_lookalike_sorting_first_is_not_read_as_the_pushed_head(w):
    beta = w["members"]["beta"]
    _lookalike(beta, beta["base"])
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    assert {m["key"]: m for m in receipt(out)["members"]}["beta"]["head_state"] == "pushed", out.stdout
    assert tip(beta["remote"], "refs/heads/feat") == beta["head"]


def test_l4_after_the_push_a_malformed_line_is_a_failure_not_a_confirmation(w, monkeypatch):
    beta = w["members"]["beta"]
    _listing(monkeypatch, lambda s: s + "not a row\n", 2)
    out = publish(w["review"])
    assert out.exit_code == 4, out.output
    row = {m["key"]: m for m in receipt(out)["members"]}["beta"]
    assert row["state"] == "failed" and row["error"].startswith("remote_unreadable: "), out.stdout


def test_c1_before_the_push_a_tabbed_row_with_no_oid_refuses_remote_unreadable(w, monkeypatch):
    beta = w["members"]["beta"]
    _listing(monkeypatch, lambda s: "garbage\trefs/heads/feat\n", 1)
    out = publish(w["review"])
    assert out.exit_code == 2, out.output
    assert receipt(out)["refused"].startswith("remote_unreadable: beta"), out.stdout
    assert untouched(beta) and untouched(w["members"]["alpha"])


def test_c2_after_the_push_a_tabbed_row_with_no_oid_fails_remote_unreadable(w, monkeypatch):
    beta = w["members"]["beta"]
    _listing(monkeypatch, lambda s: "garbage\trefs/heads/feat\n", 2)
    out = publish(w["review"])
    assert out.exit_code == 4, out.output
    row = {m["key"]: m for m in receipt(out)["members"]}["beta"]
    assert row["state"] == "failed" and row["error"].startswith("remote_unreadable: "), out.stdout
    assert row["head_state"] == "failed" and tip(beta["remote"], "refs/heads/feat") == beta["head"]
    assert {r["remote"]: r for r in receipt(out)["remotes"]}[str(beta["remote"])]["review"] != "pushed", out.stdout


@pytest.mark.parametrize("form", ["bare", "remote"])
def test_v1_a_private_member_never_reaches_a_public_members_remote(tmp_path, monkeypatch, form):
    """The review commit names every member (key, remote, range), so publishing it to a public member's remote while
    another member is private leaks the private one."""
    w = build(tmp_path, monkeypatch, visibility={"alpha": "public", "beta": "private"})
    alpha = w["members"]["alpha"]
    out = publish(w["review"]) if form == "bare" else publish(w["review"], "--remote", str(alpha["remote"]))
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output


def test_v2_an_all_public_review_still_publishes_to_a_public_members_remote(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, visibility={"alpha": "public", "beta": "public"})
    alpha = w["members"]["alpha"]
    out = publish(w["review"], "--remote", str(alpha["remote"]))
    assert out.exit_code == 0, out.output
    assert review_on(alpha["remote"], w["full"]) == w["full"]


def test_v4_a_record_without_visibility_and_more_than_one_member_now_refuses(tmp_path, monkeypatch):
    """A review bound before the field, or bound now with no visibility, has none in its record. Unknown is public, so
    the bare publish refuses until the members are declared, and the message names the field and the members."""
    w = build(tmp_path, monkeypatch, visibility={})
    out = publish(w["review"])
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output
    assert "--visibility alpha=public|private" in out.output and "'alpha', 'beta'" in out.output


def test_vo_a_pre_field_bind_record_refuses_the_bare_publish_naming_the_field_and_both_members(tmp_path, monkeypatch):
    """The old record shape: two members bound with no visibility key written anywhere in the record."""
    w = build(tmp_path, monkeypatch, visibility={})
    assert "visibility" not in git(Path.cwd(), "ls-tree", "-r", "--name-only", w["full"])
    out = publish(w["review"])
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output
    assert "'visibility' field" in out.output and "'alpha'" in out.output and "'beta'" in out.output, out.output


@pytest.mark.parametrize("owner", ["alpha", "beta"])
def test_va_an_undeclared_two_member_review_refuses_at_either_members_remote(tmp_path, monkeypatch, owner):
    """The leak: a real bind with no visibility, no patch. Neither member is declared, so neither record carries the
    field; undeclared reads public, so either member's remote refuses, with nothing pushed."""
    w = build(tmp_path, monkeypatch, visibility={})
    assert "visibility" not in git(Path.cwd(), "ls-tree", "-r", "--name-only", w["full"])
    out = publish(w["review"], "--remote", str(w["members"][owner]["remote"]))
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output
    assert "'visibility' field" in out.output, out.output


def test_vb_a_single_member_undeclared_review_still_publishes(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, visibility={}, names=("alpha",))
    out = publish(w["review"])
    assert out.exit_code == 0, out.output
    assert review_on(w["members"]["alpha"]["remote"], w["full"]) == w["full"]


def test_vc_an_all_declared_private_review_publishes_to_a_members_remote(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, visibility={"alpha": "private", "beta": "private"})
    alpha = w["members"]["alpha"]
    out = publish(w["review"], "--remote", str(alpha["remote"]))
    assert out.exit_code == 0, out.output
    assert review_on(alpha["remote"], w["full"]) == w["full"]


def test_vd_a_mixed_review_refuses_the_public_remote_and_publishes_to_the_private_one(tmp_path, monkeypatch):
    """Public content in a private remote is no leak; private content in a public remote is."""
    w = build(tmp_path, monkeypatch, visibility={"alpha": "public", "beta": "private"})
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    out = publish(w["review"], "--remote", str(alpha["remote"]))
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output
    out = publish(w["review"], "--remote", str(beta["remote"]))
    assert out.exit_code == 0, out.output
    assert review_on(beta["remote"], w["full"]) == w["full"]


def test_v3_an_empty_visibility_binds_undeclared_and_publish_refuses(tmp_path, monkeypatch):
    """proto3 reads an absent string as "", so "" is undeclared, which reads public: a two-member review refuses."""
    w = build(tmp_path, monkeypatch, visibility={"alpha": "private", "beta": ""})
    out = publish(w["review"], "--remote", str(w["members"]["alpha"]["remote"]))
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output
    assert "'visibility' field" in out.output and "'beta'" in out.output, out.output


def test_v5_a_destination_no_member_owns_that_is_not_host_local_reads_public_and_refuses(tmp_path, monkeypatch):
    """An all-private review sent to an unrelated network URL must not read private just because no member is there."""
    w = build(tmp_path, monkeypatch, visibility={"alpha": "private", "beta": "private"})
    out = publish(w["review"], "--remote", "https://example.invalid/org/elsewhere.git")
    assert all(untouched(m) for m in w["members"].values()), out.output
    assert out.exit_code == 2, out.output
    assert "not host-local; read as public" in out.output


def test_v6_a_host_local_destination_no_member_owns_still_publishes_an_all_private_review(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch, visibility={"alpha": "private", "beta": "private"})
    other = tmp_path / "other.git"
    git(tmp_path, "init", "--bare", "-b", "main", other)
    out = publish(w["review"], "--remote", str(other))
    assert out.exit_code == 0, out.output
    assert review_on(other, w["full"]) == w["full"]


@pytest.mark.parametrize("destination", ["http://example.invalid/org/repo.git", "ssh://git@example.invalid/org/repo.git",
                                         "git://example.invalid/org/repo.git", "git@example.invalid:org/repo.git"])
def test_v7_every_network_form_no_member_owns_reads_public(destination):
    from gr2.python_cli.grip import GripReviewRefused, _refuse_visibility_mismatch
    states = {"alpha": {"remote": "/srv/alpha.git", "visibility": "private"}, "beta": {"remote": "/srv/beta.git", "visibility": "private"}}
    with pytest.raises(Exception) as caught:
        _refuse_visibility_mismatch(states, destination)
    assert isinstance(caught.value, GripReviewRefused) and caught.value.refusal == "visibility_mismatch"
    assert "not host-local; read as public" in str(caught.value)


def test_v8_a_file_url_no_member_owns_reads_private(tmp_path):
    from gr2.python_cli.grip import _refuse_visibility_mismatch
    states = {"alpha": {"remote": "/srv/alpha.git", "visibility": "private"}, "beta": {"remote": "/srv/beta.git", "visibility": "private"}}
    _refuse_visibility_mismatch(states, (tmp_path / "local.git").as_uri())


def test_v9_a_file_url_with_a_host_reads_public():
    from gr2.python_cli.grip import GripReviewRefused, _refuse_visibility_mismatch
    states = {"alpha": {"remote": "/srv/alpha.git", "visibility": "private"}, "beta": {"remote": "/srv/beta.git", "visibility": "private"}}
    with pytest.raises(GripReviewRefused, match="not host-local; read as public"):
        _refuse_visibility_mismatch(states, "file://host/share/repo.git")

"""`gr2 review receive <ws> <review> --remote <r> --all` receives every member in one call: the review from <r>, each
member's pinned head from that member's own recorded remote.

Rows R1 (equals the per-member receives: same refs, same objects), R2 (one member failing: the other's row stands,
exit 4, the next command runs), R3 (count: one invocation where the per-member form needed one per member remote),
R4 (without --all nothing changes), P3/P4 (a relative or dash-led recorded remote is that member's row only and never
reaches git), P5 (a member recorded in another spelling of --remote, e.g. ssh form, is fetched as the per-member form
fetches it). Real git, bare local remotes under tmp_path, no network.
"""
from __future__ import annotations

import json
import subprocess

from gr2.python_cli import grip, review
from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.test_review_publish_all import _git_identity, build, git  # noqa: F401  (autouse fixture)

runner = make_cli_runner()


def _published(tmp_path, monkeypatch):
    w = build(tmp_path, monkeypatch)
    out = runner.invoke(app, ["review", "publish", w["review"]])
    assert out.exit_code == 0, out.output
    return w


def _reviewer(tmp_path, w, name):
    ws = tmp_path / name
    ws.mkdir()
    for key, m in w["members"].items():
        git(ws, "clone", "--no-local", "--single-branch", "--branch", "main", m["remote"], key)
    assert runner.invoke(app, ["store", "init", str(ws)]).exit_code == 0
    return ws


def _receive(ws, w, remote, *extra):
    return runner.invoke(app, ["review", "receive", str(ws), w["review"], "--remote", str(remote), *extra])


def _heads(out):
    return {h["key"]: h for h in json.loads(out.stdout)["heads"]}


def _state(ws, keys):
    """Every ref and every object in the workspace store and in each member clone (no dates, no reflogs)."""
    def one(repo):
        refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        objs = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
        return refs, objs
    return {".": one(ws), **{k: one(ws / k) for k in keys}}


def test_r1_all_equals_the_per_member_receives(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    keys = sorted(w["members"])
    looped, single = _reviewer(tmp_path, w, "looped"), _reviewer(tmp_path, w, "single")
    for k in keys:
        got = _receive(looped, w, w["members"][k]["remote"])
        assert got.exit_code == 0, got.output
    got = _receive(single, w, w["members"]["alpha"]["remote"], "--all")
    assert got.exit_code == 0, got.output
    assert {k: h["status"] for k, h in _heads(got).items()} == {"alpha": "fetched", "beta": "fetched"}, got.stdout
    for k in keys:
        assert git(single / k, "cat-file", "-t", w["members"][k]["head"]) == "commit"
    assert _state(single, keys) == _state(looped, keys)
    again = _receive(single, w, w["members"]["beta"]["remote"], "--all")  # rerun, from the other remote: idempotent
    assert again.exit_code == 0, again.output
    assert {k: h["status"] for k, h in _heads(again).items()} == {"alpha": "already_present", "beta": "already_present"}
    assert _state(single, keys) == _state(looped, keys)


def test_r2_one_member_failing_keeps_the_others_row_and_exits_4(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    alpha, beta = w["members"]["alpha"], w["members"]["beta"]
    ws = _reviewer(tmp_path, w, "reviewer")
    aside = beta["remote"].with_name("beta-aside.git")
    beta["remote"].rename(aside)  # beta's remote cannot be read
    got = _receive(ws, w, alpha["remote"], "--all")
    assert got.exit_code == 4, got.output
    rows = _heads(got)
    assert rows["alpha"]["status"] == "fetched", rows
    assert rows["beta"]["status"] == "not_fetched" and rows["beta"]["reason"].startswith("head_fetch_refused"), rows
    assert git(ws / "alpha", "cat-file", "-t", alpha["head"]) == "commit"
    assert subprocess.run(["git", "cat-file", "-e", beta["head"] + "^{commit}"], cwd=ws / "beta").returncode != 0
    assert f"fetched: alpha  head={alpha['head'][:7]}" in got.stderr, got.stderr
    assert f"not_fetched: beta  head={beta['head'][:7]}" in got.stderr, got.stderr
    assert "heads 1 of 2" in got.stderr, got.stderr
    aside.rename(beta["remote"])  # back: the same command completes
    again = _receive(ws, w, alpha["remote"], "--all")
    assert again.exit_code == 0, again.output
    assert {k: h["status"] for k, h in _heads(again).items()} == {"alpha": "already_present", "beta": "fetched"}


def test_r2b_a_missing_member_clone_is_its_row_only(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    ws = _reviewer(tmp_path, w, "reviewer")
    subprocess.run(["rm", "-rf", str(ws / "beta")], check=True)
    got = _receive(ws, w, w["members"]["alpha"]["remote"], "--all")  # read from alpha: beta's next names beta's remote
    assert got.exit_code == 4, got.output
    rows = _heads(got)
    assert rows["beta"]["status"] == "not_fetched" and rows["beta"]["reason"] == "member_clone_missing", rows
    assert rows["beta"]["next"] == f"clone {w['members']['beta']['remote']} at {ws / 'beta'}, then receive again"
    assert rows["alpha"]["status"] == "fetched", rows


def test_r3_one_invocation_where_there_were_one_per_member_remote(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    remotes = {str(m["remote"]) for m in w["members"].values()}
    assert len(remotes) == 2
    ws = _reviewer(tmp_path, w, "reviewer")
    calls = []
    real = runner.invoke

    def counted(*a, **k):
        calls.append(a[1])
        return real(*a, **k)
    monkeypatch.setattr(runner, "invoke", counted)
    got = _receive(ws, w, w["members"]["beta"]["remote"], "--all")
    assert got.exit_code == 0, got.output
    assert len(calls) == 1
    assert all(h["status"] == "fetched" for h in _heads(got).values()) and len(_heads(got)) == len(remotes)


def test_r4_without_all_the_other_members_head_is_not_this_remote(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    ws = _reviewer(tmp_path, w, "reviewer")
    got = _receive(ws, w, w["members"]["alpha"]["remote"])
    assert got.exit_code == 0, got.output
    rows = _heads(got)
    assert rows["alpha"]["status"] == "fetched" and rows["beta"]["status"] == "not_this_remote", rows
    assert got.stderr == ""


def _patch_remote(monkeypatch, key, value):
    real = grip.show_review_commit

    def fake(*a, **k):
        out = real(*a, **k)
        for m in out["members"]:
            if m["key"] == key:
                m["remote"] = value
        return out
    monkeypatch.setattr(grip, "show_review_commit", fake)


def test_p3_a_relative_member_remote_is_its_row_only(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    ws = _reviewer(tmp_path, w, "rv")
    _patch_remote(monkeypatch, "beta", "relative/beta.git")
    got = _receive(ws, w, w["members"]["alpha"]["remote"], "--all")
    rows = _heads(got)
    assert got.exit_code == 4, got.output
    assert rows["alpha"]["status"] == "fetched"
    assert rows["beta"]["reason"].startswith("member_remote_unusable"), rows


def test_p4_a_dash_led_member_remote_never_reaches_git(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    ws = _reviewer(tmp_path, w, "rv")
    _patch_remote(monkeypatch, "beta", "--upload-pack=touch /tmp/pwn519")
    seen = []
    real = grip.git

    def spy(cwd, *args, **kw):
        seen.append(args)
        return real(cwd, *args, **kw)
    monkeypatch.setattr(grip, "git", spy)
    got = _receive(ws, w, w["members"]["alpha"]["remote"], "--all")
    assert _heads(got)["beta"]["reason"].startswith("member_remote_unusable"), got.output
    assert not any("--upload-pack" in a for args in seen for a in args)
    fetches = [a for a in seen if a[:1] == ("fetch",)]
    assert fetches and all("--" not in a or a[a.index("--") + 1].startswith("/") for a in fetches), fetches


def test_p5_a_member_recorded_in_ssh_form_of_the_given_remote_is_fetched_as_the_loop_does(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    alpha = str(w["members"]["alpha"]["remote"])
    ssh = "git@github.com:o/alpha.git"
    _patch_remote(monkeypatch, "alpha", ssh)
    real = review.canonical_source_identity
    # ssh and the given remote name one repository, as github's ssh and https forms do
    monkeypatch.setattr(review, "canonical_source_identity",
                        lambda r, **k: "https://github.com/o/alpha" if r in (ssh, alpha) else real(r, **k))
    loop, one = _reviewer(tmp_path, w, "loop"), _reviewer(tmp_path, w, "one")
    a, b = _receive(loop, w, alpha), _receive(one, w, alpha, "--all")
    assert _heads(a)["alpha"]["status"] == "fetched", a.output
    assert _heads(b)["alpha"]["status"] == "fetched", b.output


def test_p6_two_uncanonical_remotes_are_not_one_repository(tmp_path, monkeypatch):
    w = _published(tmp_path, monkeypatch)
    alpha, beta = str(w["members"]["alpha"]["remote"]), str(w["members"]["beta"]["remote"])
    real = review.canonical_source_identity

    def refuse(r, **k):
        # neither remote has an identity: same() gives None for both, and None is not a repository
        if r in (alpha, beta):
            raise review.ReviewError(f"no identity for {r}")
        return real(r, **k)
    monkeypatch.setattr(review, "canonical_source_identity", refuse)
    seen = []
    real_git = grip.git

    def spy(cwd, *args, **kw):
        seen.append(args)
        return real_git(cwd, *args, **kw)
    monkeypatch.setattr(grip, "git", spy)
    ws = _reviewer(tmp_path, w, "rv")
    got = _receive(ws, w, alpha, "--all")
    rows = _heads(got)
    srcs = [a[a.index("--") + 1] for a in seen if a[:1] == ("fetch",) and "--" in a]
    head = rows["beta"]["head"]
    assert not any(a[:1] == ("fetch",) and a[-1] == head and alpha in a for a in seen), seen
    assert rows["beta"]["status"] == "fetched" and beta in srcs, (rows, srcs)


def test_a_member_path_with_a_nul_is_its_row_only_under_all(tmp_path, monkeypatch):
    """--all reads the member path from the record too: a path with a NUL is a refused row naming the path, checked
    before the path is resolved (resolving it raised ValueError out of receive), and the other member is fetched."""
    w = _published(tmp_path, monkeypatch)
    ws = _reviewer(tmp_path, w, "rv")
    real = grip.show_review_commit

    def fake(*a, **k):
        out = real(*a, **k)
        for m in out["members"]:
            if m["key"] == "beta":
                m["path"] = "b\0eta"
        return out
    monkeypatch.setattr(grip, "show_review_commit", fake)
    got = _receive(ws, w, w["members"]["alpha"]["remote"], "--all")
    assert "Traceback" not in got.output, got.output
    rows = _heads(got)
    assert rows["alpha"]["status"] == "fetched", rows
    assert rows["beta"]["status"] == "refused", rows
    assert rows["beta"]["reason"] == "member_path_control_character: 'b\\x00eta'", rows

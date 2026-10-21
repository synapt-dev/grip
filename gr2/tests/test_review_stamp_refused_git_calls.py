"""A refused `gr2 review stamp` refuses before it reads the members.

The rows record every git process gr2 starts (gitops._spawn and gitops.start) with the paths it names: its cwd, its
-C directory and every argument. A stamp with no name refuses on its first call, before the review is read. A moved
base and the bind's own author stamping need only the bind record, so they refuse before any member checkout is read:
the call count is the same at 1, 3 and 5 members, and no call names a member's checkout or its remote, except, for a
moved base, the one refused member (its remote's branches, then its checkout for the already-on-target case).
"""
from __future__ import annotations

import pytest

from gr2.python_cli import gitops, grip
from gr2.python_cli.app import app
from tests.test_review_approvals import _advance, git
from tests.test_review_transport import cli, runner

COUNTS = (1, 3, 5)

ENV = {"GIT_AUTHOR_NAME": "Author A", "GIT_AUTHOR_EMAIL": "a@example.invalid",
       "GIT_COMMITTER_NAME": "Author A", "GIT_COMMITTER_EMAIL": "a@example.invalid",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

def _world(tmp, monkeypatch, m):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    remote, seed, ws = tmp / "remote.git", tmp / "seed", tmp / "ws"
    seed.mkdir(parents=True)
    git(seed, "init", "-b", "main")
    (seed / "payload").write_text("base\n")
    git(seed, "add", "payload"); git(seed, "commit", "-m", "base")
    base = git(seed, "rev-parse", "HEAD")
    ws.mkdir()
    keys = [f"member{i}" for i in range(m)]
    for k in keys:
        r = tmp / f"{k}.git"
        git(tmp, "init", "--bare", "-b", "main", r)
        git(seed, "push", r, "main")
        git(ws, "clone", r, k)
    assert runner.invoke(app, ["store", "init", str(ws)]).exit_code == 0
    rows = []
    for k in keys:
        mem = ws / k
        git(mem, "checkout", "-b", "feature")
        (mem / "payload").write_text(f"r {k}\n")
        git(mem, "add", "payload"); git(mem, "commit", "-m", k)
        head = git(mem, "rev-parse", "HEAD")
        git(mem, "push", tmp / f"{k}.git", "HEAD:refs/heads/feature")
        rows.append(dict(key=k, path=k, remote=str(tmp / f"{k}.git"), base=base, head=head, ref="refs/heads/main",
                         title=k, body="", source=str(mem), visibility="public"))
    git(ws, "config", "user.name", "Author A"); git(ws, "config", "user.email", "a@example.invalid")
    rid = grip.create_review_bind_commit(ws, rows)
    for k in keys:
        pub = cli(ws, monkeypatch, "publish", "gr:" + rid, "--remote", tmp / f"{k}.git")
        assert pub.exit_code == 0, pub.output
    monkeypatch.chdir(ws)
    return dict(ws=ws, remote=tmp / "member0.git", base=base, rid=rid, member=ws / keys[0])

def counted(monkeypatch, argv):
    calls = []
    spawn, start = gitops._spawn, gitops.start
    def c(a, **kw):
        calls.append(gitops.command_of(a)); return spawn(a, **kw)
    def s(cwd, *a, **kw):
        calls.append(" ".join(a)); return start(cwd, *a, **kw)
    monkeypatch.setattr(gitops, "_spawn", c); monkeypatch.setattr(gitops, "start", s)
    r = runner.invoke(app, argv)
    monkeypatch.setattr(gitops, "_spawn", spawn); monkeypatch.setattr(gitops, "start", start)
    return r, calls

def _stamp_calls(tmp_path, monkeypatch, m, case):
    w = _world(tmp_path / f"{case}{m}", monkeypatch, m)
    if case == "moved":
        _advance(w["member"], w["remote"], w["base"])
    name = {"moved": "Approver B", "self": "Author A", "noname": ""}[case]
    # The stamp's name is GIT_AUTHOR_NAME, else the workspace user.name.
    git(w["ws"], "config", "user.name", name)
    monkeypatch.delenv("GIT_AUTHOR_NAME") if not name else monkeypatch.setenv("GIT_AUTHOR_NAME", name)
    calls = []
    spawn, start = gitops._spawn, gitops.start

    def counted(argv, **kw):
        calls.append([str(a) for a in argv] + [str(kw.get("cwd") or "")])
        return spawn(argv, **kw)

    def started(cwd, *args, **kw):
        calls.append(["git", "-C", str(cwd), *map(str, args)])
        return start(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "_spawn", counted)
    monkeypatch.setattr(gitops, "start", started)
    result = runner.invoke(app, ["review", "stamp", str(w["ws"]), "gr:" + w["rid"]])
    monkeypatch.setattr(gitops, "_spawn", spawn)
    monkeypatch.setattr(gitops, "start", start)
    assert result.exit_code == 2, result.output
    code = {"moved": "base_moved: member0:", "self": "self_approval", "noname": "approval_identity_missing"}[case]
    assert result.output.splitlines()[0].startswith(code), result.output
    return w, calls


def _members_named(tmp, ws, m, call):
    """The members whose checkout or remote this git call names (in its cwd, -C path or any argument)."""
    paths = {f"member{i}": (str(ws / f"member{i}"), str(tmp / f"member{i}.git")) for i in range(m)}
    return {k for k, ps in paths.items() for x in call for p in ps if x == p or x.startswith(p + "/")}


@pytest.mark.parametrize("case", ["moved", "self"])
def test_a_stamp_refused_on_the_bind_makes_the_same_git_calls_at_any_member_count(tmp_path, monkeypatch, case):
    counts = {m: len(_stamp_calls(tmp_path, monkeypatch, m, case)[1]) for m in COUNTS}
    print(f"refused stamp ({case}) git calls {counts}")
    assert len(set(counts.values())) == 1, counts


@pytest.mark.parametrize("case", ["moved", "self"])
@pytest.mark.parametrize("m", COUNTS)
def test_a_stamp_refused_on_the_bind_names_no_member_checkout_or_remote(tmp_path, monkeypatch, case, m):
    w, calls = _stamp_calls(tmp_path, monkeypatch, m, case)
    tmp = tmp_path / f"{case}{m}"
    named = [(sorted(k), c) for c in calls if (k := _members_named(tmp, w["ws"], m, c))]
    allowed = {"member0"} if case == "moved" else set()
    assert [n for n in named if not set(n[0]) <= allowed] == [], named
    if case == "moved":
        assert any(c[c.index("ls-remote"):][:2] == ["ls-remote", "--symref"] for _, c in named if "ls-remote" in c)


def test_a_stamp_with_no_name_refuses_on_its_first_git_call(tmp_path, monkeypatch):
    counts = {m: len(_stamp_calls(tmp_path, monkeypatch, m, "noname")[1]) for m in (1, 5)}
    print(f"refused stamp (noname) git calls {counts}")
    assert counts == {1: 1, 5: 1}, counts


def test_a_fault_in_the_moved_members_read_keeps_the_base_moved_refusal(tmp_path, monkeypatch):
    # The on-target skip reads the refused member's checkout; when that read faults (here the checkout is gone),
    # the stamp refuses with the original base_moved, never with the read's own refusal and never as a skip.
    import shutil
    w = _world(tmp_path, monkeypatch, 1)
    _advance(w["member"], w["remote"], w["base"])
    shutil.rmtree(w["member"])
    git(w["ws"], "config", "user.name", "Approver B")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Approver B")
    result = runner.invoke(app, ["review", "stamp", str(w["ws"]), "gr:" + w["rid"]])
    assert result.exit_code == 2, result.output
    assert result.output.splitlines()[0].startswith("base_moved: member0:"), result.output


@pytest.mark.parametrize("m", [1, 3])
def test_a_successful_stamp_reads_each_members_remote_once_before_it_pushes(tmp_path, monkeypatch, m):
    # Before the approval is pushed, a stamp reads each member's remote branches once (the moved-base read) and
    # each member's approval chain once; the count that follows the push is its own step and is not counted here.
    w = _world(tmp_path, monkeypatch, m)
    git(w["ws"], "config", "user.name", "Approver B")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Approver B")
    argvs = []
    spawn = gitops._spawn

    def counted(argv, **kw):
        argvs.append([str(a) for a in argv])
        return spawn(argv, **kw)

    monkeypatch.setattr(gitops, "_spawn", counted)
    result = runner.invoke(app, ["review", "stamp", str(w["ws"]), "gr:" + w["rid"]])
    monkeypatch.setattr(gitops, "_spawn", spawn)
    assert result.exit_code == 0, result.output
    commands = [gitops.command_of(a) for a in argvs]
    before = argvs[:commands.index("push")]
    reads = [a for a in before if gitops.command_of(a) in ("ls-remote", "fetch")]
    branch_reads = sorted(a[a.index("--") + 1] for a in reads if "--symref" in a)
    remotes = sorted(str(tmp_path / f"member{i}.git") for i in range(m))
    assert branch_reads == remotes, branch_reads
    assert len(reads) == 2 * m, [a[a.index(gitops.command_of(a)):] for a in reads]


def test_a_stamper_whose_member_remote_lacks_the_head_still_refuses_head_not_fetched(tmp_path, monkeypatch):
    # The early refusals moved before the member loop leave this one where it was: a stamper who is not the bind's
    # author, whose second member's checkout and remote both lack the reviewed head, gets head_not_fetched by name
    # with the next step, never a raw git error and never a pass.
    import subprocess
    w = _world(tmp_path, monkeypatch, 2)
    mem, remote = w["ws"] / "member1", tmp_path / "member1.git"
    head = git(mem, "rev-parse", "HEAD")
    git(mem, "checkout", "-q", "main"); git(mem, "branch", "-D", "feature")
    for r in (mem, remote):
        for ref in git(r, "for-each-ref", "--format=%(refname)").splitlines():
            if ref not in ("refs/heads/main", "refs/remotes/origin/main", "refs/remotes/origin/HEAD"):
                git(r, "update-ref", "-d", ref)
        git(r, "reflog", "expire", "--expire=now", "--all"); git(r, "gc", "-q", "--prune=now")
        assert subprocess.run(["git", "-C", str(r), "cat-file", "-e", head + "^{commit}"],
                              capture_output=True).returncode != 0, f"precondition: {r} lacks the head"
    git(w["ws"], "config", "user.name", "Approver B")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Approver B")
    result = runner.invoke(app, ["review", "stamp", str(w["ws"]), "gr:" + w["rid"]])
    assert result.exit_code == 2, result.output
    assert result.output.splitlines()[0] == "head_not_fetched: member1", result.output
    assert "does not serve the reviewed head" in result.output and "stamp again" in result.output, result.output
    assert "unknown revision" not in result.output and "fatal:" not in result.output, result.output

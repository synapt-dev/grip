"""gr2 review merge reads each member remote once per moment: one ls-remote and one fetch by id, no private ref.

The pre-merge read of a member (its target, its feature and its published review) was three ls-remotes, two fetches
into private refs and two update-ref -d; every later re-read was one of each per ref. Counted at the process seam
(gitops._spawn + gitops.start), m = 1, 3, 5."""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import check_records, gitops
from gr2.python_cli.app import app

runner = CliRunner()


def _git(root, *args):
    p = subprocess.run(["git", *map(str, args)], cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout.strip()


def _world(d: Path, m: int, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    home = d / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    acfg = home / "acfg"; monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(acfg))
    _git(home, "config", "--global", "user.name", "Author"); _git(home, "config", "--global", "user.email", "a@example.invalid")
    author, recv = d / "author", d / "recv"; author.mkdir(); recv.mkdir()
    rows, mem = [], {}
    for i in range(m):
        n = f"m{i}"; remote, seed = d / f"{n}.git", d / f"seed-{n}"
        _git(d, "init", "-q", "--bare", "-b", "main", remote); seed.mkdir(); _git(seed, "init", "-q", "-b", "main")
        (seed / "p.txt").write_text(f"{n} base\n"); _git(seed, "add", "p.txt"); _git(seed, "commit", "-qm", "base")
        base = _git(seed, "rev-parse", "HEAD"); _git(seed, "push", "-q", remote, "main")
        _git(author, "clone", "-q", "--no-local", "--branch", "main", remote, n)
        _git(recv, "clone", "-q", "--no-local", "--branch", "main", remote, n)
        r = author / n; _git(r, "checkout", "-qb", "feat"); (r / "p.txt").write_text(f"{n} reviewed\n")
        _git(r, "commit", "-qam", "reviewed"); head = _git(r, "rev-parse", "HEAD"); _git(r, "push", "-q", remote, "feat")
        mem[n] = dict(remote=remote, head=head)
        rows.append(dict(key=n, path=n, remote=str(remote), base=base, head=head, ref="refs/heads/main", source=str(r)))
    (d / "rows.json").write_text(json.dumps(rows))
    for ws in (author, recv):
        assert runner.invoke(app, ["store", "init", str(ws)]).exit_code == 0
    monkeypatch.chdir(author)
    assert runner.invoke(app, ["review", "bind", "--rows-json", str(d / "rows.json")]).exit_code == 0
    rref = [x for x in _git(author, "for-each-ref", "--format=%(refname)").splitlines() if "review" in x][0]
    rid = "gr:" + rref.rsplit("/", 1)[1]
    for n, v in mem.items():
        assert runner.invoke(app, ["review", "publish", rid, "--remote", str(v["remote"])]).exit_code == 0
        check_records.run_check(author / n, str(v["remote"]), v["head"], "test", [sys.executable, "-c", "pass"])
    assert runner.invoke(app, ["check", "set", str(author), rid, "--", sys.executable, "-c", "pass"]).exit_code == 0
    monkeypatch.chdir(recv)
    rcfg = home / "rcfg"; _git(home, "config", "--file", rcfg, "user.name", "Reviewer")
    _git(home, "config", "--file", rcfg, "user.email", "r@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(rcfg))
    for v in mem.values():
        assert runner.invoke(app, ["review", "receive", str(recv), rid, "--remote", str(v["remote"])]).exit_code == 0
    assert runner.invoke(app, ["review", "stamp", str(recv), rid]).exit_code == 0
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(acfg))
    return author, rid, mem


def _merge_counted(author, rid, monkeypatch):
    calls = collections.Counter()
    spawn, start = gitops._spawn, gitops.start

    def counted(argv, *a, **k):
        av = [str(x) for x in argv]
        verb = next((x for x in av[1:] if x in ("ls-remote", "fetch", "update-ref")), None)
        if verb:
            own = [f.filename for f in traceback.extract_stack() if "python_cli" in f.filename and not f.filename.endswith("gitops.py")]
            gate = own[-1].endswith("merge_gate.py")  # issued by the gate itself, not by approvals or check_records
            calls[(verb, "-d" in av if verb == "update-ref" else False, gate)] += 1
        calls["all"] += 1
        return spawn(argv, *a, **k)

    def started(cwd, *args, **kw):
        calls["all"] += 1
        return start(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "_spawn", counted); monkeypatch.setattr(gitops, "start", started)
    monkeypatch.chdir(author)
    r = runner.invoke(app, ["review", "merge", str(author), rid, "--into", "main", "--from", "feat", "--approvals", "1"])
    monkeypatch.setattr(gitops, "_spawn", spawn); monkeypatch.setattr(gitops, "start", start)
    return r, calls


@pytest.mark.parametrize("m", [1, 3, 5])
def test_merge_reads_each_remote_once_per_moment_with_no_private_ref(tmp_path, monkeypatch, m):
    author, rid, mem = _world(tmp_path, m, monkeypatch)
    before = {n: _git(author / n, "for-each-ref", "--format=%(refname)") for n in mem}
    r, calls = _merge_counted(author, rid, monkeypatch)
    assert r.exit_code == 0, r.output[-1500:]
    for n, v in mem.items():  # merged heads: main on each remote is a merge whose second parent is the reviewed head
        assert _git(tmp_path, "--git-dir", v["remote"], "rev-parse", "main^2") == v["head"]
    # the gate's own reads: per member 2 (pre-merge) + 2 (before push) + 4 (two re-reads after push)
    gate_ls = calls[("ls-remote", False, True)]; gate_fetch = calls[("fetch", False, True)]
    assert (gate_ls, gate_fetch) == (4 * m, 4 * m + m), dict(calls)  # + m: the tracking-ref refresh fetch
    assert calls[("update-ref", True, True)] == 0, dict(calls)
    after = {n: _git(author / n, "for-each-ref", "--format=%(refname)") for n in mem}
    assert after == before  # the same ref names: no private ref made, none left behind

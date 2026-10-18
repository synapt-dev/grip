"""Ancestry answers ignore a forged commit-graph and a caller's graft file.
1. A forged commit-graph file that gives the tip's parent P the unrelated root X as a parent makes
   merge-base --is-ancestor X tip answer yes, under GIT_NO_REPLACE_OBJECTS=1 + GIT_GRAFT_FILE=/dev/null.
   The ancestry call passes `-c core.commitGraph=false` so the walk reads the commit objects.
2. A GIT_GRAFT_FILE the CALLER's environment already carries must not win: kills the mutant
   env={**REAL_HISTORY_ENV, **os.environ}."""
import os, struct, subprocess
from gr2.python_cli import gitops

def _g(w, *a, env=None):
    return subprocess.run(["git", "-C", str(w), *a], capture_output=True, text=True, check=True, env=env).stdout.strip()

def _world(tmp_path):
    w = tmp_path / "w"; w.mkdir(); _g(w, "init", "-q", "-b", "main")
    e = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}
    def c(msg, date):
        (w / "f").write_text(msg); _g(w, "add", "f", env=e)
        _g(w, "commit", "-qm", msg, env={**e, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return _g(w, "rev-parse", "HEAD")
    x = c("x", "1000000000 +0000")
    _g(w, "checkout", "-q", "--orphan", "feat"); _g(w, "rm", "-qrf", ".")
    p = c("p", "1000000100 +0000"); t = c("t", "1000000200 +0000")
    return w, x, p, t

def _forge(w, x, p):
    """Write w's commit-graph, then edit p's first parent in CDAT to x."""
    _g(w, "commit-graph", "write", "--reachable")
    cg = w / ".git" / "objects" / "info" / "commit-graph"; b = bytearray(cg.read_bytes())
    ch = {bytes(b[8 + 12 * i:12 + 12 * i]): struct.unpack(">Q", b[12 + 12 * i:20 + 12 * i])[0] for i in range(b[6] + 1)}
    oidl, cdat = ch[b"OIDL"], ch[b"CDAT"]
    oids = [bytes(b[oidl + 20 * i:oidl + 20 * i + 20]).hex() for i in range((cdat - oidl) // 20)]
    off = cdat + 36 * oids.index(p) + 20
    b[off:off + 4] = struct.pack(">I", oids.index(x))
    os.chmod(cg, 0o644); cg.write_bytes(bytes(b))

def test_forged_commit_graph_parent_is_not_an_ancestor(tmp_path):
    w, x, p, t = _world(tmp_path)
    _forge(w, x, p)
    # The probe is real: plain git reads x as an ancestor of t through the forged graph.
    assert subprocess.run(["git", "-C", str(w), "merge-base", "--is-ancestor", x, t], capture_output=True).returncode == 0
    assert gitops.is_ancestor(w, x, t) is False
    assert gitops.is_ancestor(w, x, t, via_cwd=True) is False
    assert gitops.ancestry_proc(w, x, t).returncode == 1
    assert gitops.is_ancestor(w, p, t) is True

def test_caller_env_graft_file_does_not_win(tmp_path, monkeypatch):
    w, x, p, t = _world(tmp_path)
    forged = tmp_path / "grafts"; forged.write_text(f"{t} {x}\n")
    monkeypatch.setenv("GIT_GRAFT_FILE", str(forged))
    assert subprocess.run(["git", "-C", str(w), "merge-base", "--is-ancestor", x, t], capture_output=True).returncode == 0
    assert gitops.is_ancestor(w, x, t) is False
    assert gitops.is_ancestor(w, p, t) is True

def test_forged_commit_graph_in_an_alternate_is_not_read(tmp_path):
    """A repo that borrows objects through objects/info/alternates also reads the alternate's commit-graph."""
    w, x, p, t = _world(tmp_path)
    _forge(w, x, p)
    b = tmp_path / "borrower"; b.mkdir(); _g(b, "init", "-q")
    (b / ".git" / "objects" / "info" / "alternates").write_text(str(w / ".git" / "objects") + "\n")
    assert not (b / ".git" / "objects" / "info" / "commit-graph").exists()
    assert subprocess.run(["git", "-C", str(b), "merge-base", "--is-ancestor", x, t], capture_output=True).returncode == 0
    assert gitops.is_ancestor(b, x, t) is False
    assert gitops.is_ancestor(b, p, t) is True

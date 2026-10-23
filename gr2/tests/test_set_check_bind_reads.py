"""A check over a set reads the bind store a fixed number of times, however many members the set holds."""
import collections
import sys

from gr2.python_cli import gitops, set_check
from tests.test_review_merge_gate import _git_identity, make_slice  # noqa: F401


def _bind_store_spawns(tmp_path, monkeypatch, names):
    tmp_path.mkdir()
    made = make_slice(tmp_path, monkeypatch, names)
    ws, calls = made["author"], collections.Counter()
    spawn = gitops._spawn

    def counted(argv, **kw):
        if str(kw.get("cwd")) == str(ws):
            calls[gitops.command_of(argv)] += 1
        return spawn(argv, **kw)

    monkeypatch.setattr(gitops, "_spawn", counted)
    out = set_check.run_set_check(ws, made["review"], "set", [sys.executable, "-c", "pass"])
    monkeypatch.setattr(gitops, "_spawn", spawn)
    assert [m["key"] for m in out["members"]] == sorted(names)
    return calls


def test_bind_store_reads_do_not_grow_with_members(tmp_path, monkeypatch):
    two = _bind_store_spawns(tmp_path / "two", monkeypatch, ("alpha", "beta"))
    three = _bind_store_spawns(tmp_path / "three", monkeypatch, ("alpha", "beta", "gamma"))
    print("two", dict(two), "three", dict(three))
    assert three == two

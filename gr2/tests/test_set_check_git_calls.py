"""`check set` reads each member's check store once, however many records the store already holds.

The witness counts every git process gr2 starts during one `check set` over a two-member review (gitops._spawn and
gitops.start, the places gr2 starts git), after N earlier records at other heads on each member's store. A run that
re-reads every store entry per process (base f8cb2d16: one `cat-file blob` per entry, read twice, a `rev-parse
--show-object-format` per entry, and a `hash-object` per entry) grows with N; one that reads the store tip through
one `cat-file --batch` and keeps the blob ids of entries it did not change stays flat.
"""
from __future__ import annotations

import sys

from gr2.python_cli import check_records, gitops, set_check
from tests.test_review_merge_gate import _git_identity, git, slice2  # noqa: F401  (fixtures)

SLACK = 6  # calls a run at N=20 may spend beyond the same run at N=1


def _prior(s, upto):
    """Each member's store gains records at fresh heads until it holds `upto` of them beside the review's own."""
    for m in s["members"].values():
        have = m.setdefault("prior", 0)
        tree = git(m["repo"], "rev-parse", m["head"] + "^{tree}")
        for i in range(have, upto):
            head = git(m["repo"], "commit-tree", tree, "-p", m["head"], "-m", f"prior {i}")
            check_records.publish_observation(m["repo"], str(m["remote"]),
                                              dict(v=1, head=head, observed_head=head, name="test",
                                                   result="pass", exit_code=0))
        m["prior"] = upto


def _set_calls(s, monkeypatch):
    calls = []
    spawn, start = gitops._spawn, gitops.start

    def counted(argv, **kw):
        calls.append(gitops.command_of(argv))
        return spawn(argv, **kw)

    def started(cwd, *args):
        calls.append(" ".join(args))
        return start(cwd, *args)

    monkeypatch.setattr(gitops, "_spawn", counted)
    monkeypatch.setattr(gitops, "start", started)
    try:
        rec = set_check.run_set_check(s["author"], s["review"], "set", [sys.executable, "-c", "pass"])
    finally:
        monkeypatch.setattr(gitops, "_spawn", spawn)
        monkeypatch.setattr(gitops, "start", start)
    assert rec.get("result") == "pass", rec
    return calls


def test_check_set_git_calls_do_not_grow_with_the_records_already_stored(slice2, monkeypatch):
    counts = {}
    for n in (1, 5, 20):
        _prior(slice2, n)
        counts[n] = len(_set_calls(slice2, monkeypatch))
    print(f"check set git calls {counts}")
    assert counts[20] - counts[1] <= SLACK, counts

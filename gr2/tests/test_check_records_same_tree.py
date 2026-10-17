"""Publishing check records writes the same store tree, byte for byte, as the per-entry reader did (dev f8cb2d16).

A fixed history (fixed commit dates, so fixed heads) publishes records at several heads, a second record and a
second name at one head, then reads them back. The store tree id at the remote is pinned to the id that f8cb2d16
wrote for the same records: reusing an unchanged entry's blob and writing every fanout tree in one batch must not
change a byte.
"""
from __future__ import annotations

import subprocess

from gr2.python_cli import check_records

PINNED_TREE = "d4a2a25633451dd2101fc8909d81db984771c859"


def _git(cwd, *args, env):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, env=env, check=True, capture_output=True, input="",
                          text=True).stdout.strip()


def test_the_store_tree_matches_the_tree_the_per_entry_reader_wrote(tmp_path, monkeypatch):
    stamp = "2026-10-17T00:00:00Z"
    for name, value in (("GIT_AUTHOR_DATE", stamp), ("GIT_COMMITTER_DATE", stamp), ("GIT_AUTHOR_NAME", "t"),
                        ("GIT_AUTHOR_EMAIL", "t@example.invalid"), ("GIT_COMMITTER_NAME", "t"),
                        ("GIT_COMMITTER_EMAIL", "t@example.invalid"), ("GIT_CONFIG_NOSYSTEM", "1")):
        monkeypatch.setenv(name, value)
    import os
    env = dict(os.environ)
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    _git(tmp_path, "init", "--bare", "-b", "main", remote, env=env)
    _git(tmp_path, "init", "-b", "main", repo, env=env)
    tree = _git(repo, "mktree", env=env)
    heads, parent = [], []
    for i in range(12):
        head = _git(repo, "commit-tree", tree, *parent, "-m", f"head {i}", env=env)
        heads.append(head)
        parent = ["-p", head]
    for i, head in enumerate(heads):
        check_records.publish_observation(repo, str(remote), dict(v=1, head=head, observed_head=head, name="test",
                                                                 result="pass", exit_code=0))
    check_records.publish_observation(repo, str(remote), dict(v=1, head=heads[3], observed_head=heads[3],
                                                              name="test", result="fail", exit_code=1))
    check_records.publish_observation(repo, str(remote), dict(v=1, head=heads[3], observed_head=heads[3],
                                                              name="set", result="pass", exit_code=0))
    got = check_records.read_remote_check(str(remote), {"path": repo, "key": "r", "remote": str(remote)}, heads[3],
                                          ("set",))
    assert sorted((r["name"], r["result"]) for r in got["records"]) == [("set", "pass"), ("test", "fail"),
                                                                    ("test", "pass")], got
    assert _git(remote, "rev-parse", check_records.CHECK_REF + "^{tree}", env=env) == PINNED_TREE


def _store_with_empty_fanout(tmp_path, monkeypatch, with_record):
    """A remote check store whose tree carries a fanout tree with no leaves, beside a valid record or alone."""
    import os
    for name, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
                        ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.invalid"),
                        ("GIT_CONFIG_NOSYSTEM", "1")):
        monkeypatch.setenv(name, value)
    env = dict(os.environ)
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    _git(tmp_path, "init", "--bare", "-b", "main", remote, env=env)
    _git(tmp_path, "init", "-b", "main", repo, env=env)
    head = _git(repo, "commit-tree", _git(repo, "mktree", env=env), "-m", "head", env=env)
    rows = []
    if with_record:
        check_records.publish_observation(repo, str(remote), dict(v=1, head=head, observed_head=head, name="test",
                                                                 result="pass", exit_code=0))
        rows = _git(remote, "ls-tree", check_records.CHECK_REF + "^{tree}", env=env).splitlines()
        assert rows, "the store holds no record before the empty fanout is added"
    empty = _git(remote, "mktree", env=env)
    fanout = next(f"{a}{b}" for a in "0123456789abcdef" for b in "0123456789abcdef"
                  if not any(row.endswith("\t" + a + b) for row in rows) and a + b != head[:2])
    tree = subprocess.run(["git", "mktree"], cwd=remote, env=env, check=True, capture_output=True, text=True,
                          input="".join(row + "\n" for row in rows + [f"040000 tree {empty}\t{fanout}"])).stdout.strip()
    _git(remote, "update-ref", check_records.CHECK_REF, _git(remote, "commit-tree", tree, "-m", "empty fanout", env=env),
         env=env)
    return remote, repo, head


def test_a_store_holding_only_an_empty_fanout_tree_is_refused(tmp_path, monkeypatch):
    remote, repo, head = _store_with_empty_fanout(tmp_path, monkeypatch, with_record=False)
    got = check_records.read_remote_check(str(remote), {"path": repo, "key": "r", "remote": str(remote)}, head)
    assert (got["status"], got["reason"], got["records"]) == ("fail", "empty_checks_fanout", []), got


def test_an_empty_fanout_tree_beside_a_valid_record_is_refused(tmp_path, monkeypatch):
    remote, repo, head = _store_with_empty_fanout(tmp_path, monkeypatch, with_record=True)
    got = check_records.read_remote_check(str(remote), {"path": repo, "key": "r", "remote": str(remote)}, head)
    assert (got["status"], got["reason"], got["records"]) == ("fail", "empty_checks_fanout", []), got

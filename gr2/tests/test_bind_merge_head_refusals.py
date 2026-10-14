"""`review bind` refuses two merge-head ranges that check set would otherwise mis-read.

A range that holds a merge is carried as one tree diff (base..head), so its reconstruction is the head's tree. Two shapes
of that make the carry wrong in a way nothing reports at bind:
  - the head does not contain the bound base (the author merged main, main moved again, and the author re-pinned on
    the new main without merging again): the rebuilt tree lacks what main gained, and a passing set check would attest
    a combination nobody tested. Refused `base_not_ancestor_of_head`, the code review merge already uses.
  - the head's tree equals the base's (both authors made the same change): the carried patch is empty and the rebuild
    used to fail inside git. Refused `range_adds_nothing`.

Rows, and what turns each red:
  stale merge row:   refused base_not_ancestor_of_head, with a next line   -> drop the ancestry check
  merged-again row:  the same world after merging main again binds         -> refuse every merge range
  empty net row:     refused range_adds_nothing, never git's own text      -> drop the empty check
"""
from __future__ import annotations

import subprocess

from typer.testing import CliRunner

from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX  # noqa: F401
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (autouse identity fixture)

runner = CliRunner()


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    remote, seed, author = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "author"
    git(tmp_path, "init", "--bare", "-b", "main", remote)
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "payload.txt").write_text("line one\nshared line\nline three\n")
    git(seed, "add", "payload.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "push", remote, "main")
    author.mkdir()
    git(author, "clone", "--no-local", "--branch", "main", remote, "member")
    init = runner.invoke(app, ["store", "init", str(author)])
    assert init.exit_code == 0, init.output
    member = author / "member"
    git(member, "checkout", "-b", "feat")
    return remote, seed, author, member


def _land(seed, remote, name, text, message):
    (seed / name).write_text(text)
    git(seed, "add", name)
    git(seed, "commit", "-m", message)
    git(seed, "push", remote, "main")
    return git(seed, "rev-parse", "HEAD")


def _merge_main(member, resolved=None):
    p = subprocess.run(["git", "merge", "--no-edit", "origin/main"], cwd=member, capture_output=True, text=True)
    if resolved is not None:
        assert p.returncode == 1 and "CONFLICT" in p.stdout, (p.stdout, p.stderr)
        (member / "payload.txt").write_text(resolved)
        git(member, "add", "payload.txt")
        git(member, "commit", "--no-edit", "-m", "merge main, resolved")
    else:
        assert p.returncode == 0, (p.stdout, p.stderr)


def _bind(author, member, remote, base):
    return runner.invoke(app, ["review", "bind", "--repo", "member", "--remote", str(remote), "--base", base,
                               "--head", git(member, "rev-parse", "HEAD"), "--ref", "refs/heads/main",
                               "--source", str(member)])


def _stale_merge_world(tmp_path, monkeypatch):
    """F2 merged F1's main and resolved; then F3 landed on main. Returns (author, member, remote, seed, F3's main)."""
    remote, seed, author, member = _seed(tmp_path, monkeypatch)
    (member / "payload.txt").write_text("line one\nshared line, F2\nline three\n")
    git(member, "commit", "-am", "F2")
    _land(seed, remote, "payload.txt", "line one\nshared line, F1\nline three\n", "F1")
    git(member, "fetch", "origin")
    _merge_main(member, resolved="line one\nshared line, F1 and F2\nline three\n")
    f3 = _land(seed, remote, "f3.txt", "added after F2 merged main\n", "F3")
    git(member, "fetch", "origin")
    monkeypatch.chdir(author)
    return author, member, remote, seed, f3


def test_a_merge_head_without_the_current_base_is_refused_at_bind(tmp_path, monkeypatch):
    author, member, remote, _seed_dir, f3 = _stale_merge_world(tmp_path, monkeypatch)
    out = _bind(author, member, remote, f3)
    assert out.exit_code == 2, out.output
    assert "refused: base_not_ancestor_of_head" in out.output, out.output
    assert "next:" in out.output and "merge" in out.output.split("next:", 1)[1], out.output


def test_the_same_world_merged_again_binds(tmp_path, monkeypatch):
    author, member, remote, _seed_dir, f3 = _stale_merge_world(tmp_path, monkeypatch)
    _merge_main(member)
    out = _bind(author, member, remote, f3)
    assert out.exit_code == 0, out.output


def test_a_merge_range_that_adds_nothing_is_named_at_bind(tmp_path, monkeypatch):
    remote, seed, author, member = _seed(tmp_path, monkeypatch)
    same = "line one\nshared line, the same fix\nline three\n"
    (member / "payload.txt").write_text(same)
    git(member, "commit", "-am", "F2: the fix")
    base = _land(seed, remote, "payload.txt", same, "F1: the same fix")
    git(member, "fetch", "origin")
    _merge_main(member)
    assert git(member, "rev-parse", "HEAD^{tree}") == git(member, "rev-parse", f"{base}^{{tree}}")
    monkeypatch.chdir(author)
    out = _bind(author, member, remote, base)
    assert out.exit_code == 2, out.output
    assert "refused: range_adds_nothing" in out.output, out.output
    assert "No valid patches" not in out.output, out.output

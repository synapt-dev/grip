"""A merge over five members reads each member remote once per moment (one ls-remote and one fetch by id, no
temporary ref to make and delete per review ref), so its git process count stays bounded: counted by a shim in
front of git on PATH that logs one line per exec, not by reading the source."""
from __future__ import annotations

import collections
import os
import shlex
import shutil
import subprocess

from gr2.python_cli import merge_gate
from tests.test_review_merge_gate import _git_identity, make_slice, record_set  # noqa: F401  (fixtures)

# Measured on the merge that reads each remote once per moment: 197 execs at five members (55 rev-parse, 30 fetch,
# 25 ls-remote, ...). A per-review-ref ls-remote, fetch and update-ref -d loop puts it well above.
MAX_GIT_EXECS = 197


def _subcommand(argv: list[str]) -> str:
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-c", "-C") else 1
    return argv[i] if i < len(argv) else "?"


def test_a_five_member_merge_spawns_a_bounded_number_of_git_processes(tmp_path, monkeypatch):
    s = make_slice(tmp_path, monkeypatch, ("a1", "a2", "a3", "a4", "a5"))
    record_set(s)
    real = shutil.which("git")
    bin_dir, log = tmp_path / "shim-bin", tmp_path / "git-exec.log"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(log))}\nexec {shlex.quote(real)} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    assert log.read_text() == "--version\n", "git on PATH must be the counting shim"
    log.unlink()
    code, receipt = merge_gate.review_merge(s["author"], s["review"], feature="feat", required_approvals=0)
    assert code == merge_gate.EXIT_MERGED, receipt
    lines = log.read_text().splitlines()
    by = collections.Counter(_subcommand(line.split(" ")) for line in lines)
    assert len(lines) <= MAX_GIT_EXECS, f"{len(lines)} git execs: {dict(by.most_common())}"

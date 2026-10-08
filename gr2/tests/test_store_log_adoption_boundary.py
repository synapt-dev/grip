"""2026-09-28: `store log` on an ADOPTED root stops at the adoption boundary.

THE DEFECT, measured on 984cbb0e before this range:
  A root that carried its own history BEFORE it became a store -- an ADOPTED root -- has
  commits that predate `grip.toml`. `_native_store_log` walked every commit from `rev-list`
  and read the document at each one, so the first pre-adoption commit produced
      fatal: path 'grip.toml' exists on disk, but not in '<sha>'
  and took the WHOLE verb down: exit 5, no rows, on a root whose store was otherwise working.

  ⚠ THIS DOCSTRING ONCE CLAIMED MORE THAN IT MEASURED. An earlier version said the same
  fatal also "poisoned the store forward -- the next `store commit` exited 5 as well". That
  causal link is FALSE, and this suite's own control disproves it. On the base arm with the
  guard disabled and a LEGITIMATE second commit -- the member advanced AND pushed to its
  origin first, which R2 does -- commit1 exits 0, commit2 exits 0, and only `log` exits 5.
  The second exit 5 was the NO-CHANGE commit, which fails identically on an unadopted root.
  A no-change `store commit` never had a member change to record, so it runs `git commit`
  with nothing staged and git refuses: that is a separate defect, not this boundary's.

THE SPECIFIED SHAPE: `log` stops at the adoption boundary (the first commit without
`grip.toml`), shows everything after it, and never fails whole.

FIXTURES ARE REAL GIT, no mocks and no network: each member has a bare local origin, and the
adopted root is built the way a real one is -- `git init`, some plain commits, and only THEN
the store. That ordering is the entire point: it is the only way to produce a commit that
predates the document.

REVIEW INSTRUMENT NOTE, carried here and in the freeze: reproduce with `store log` and NO
positional argument, on a fresh clone with its OWN venv, and print the import path first.
`store log .` (with a positional) takes the LEGACY `_read_snapshot_index` branch and returns
exit 1 "No .grip/ directory", which is a different and milder defect -- the native branch is
the no-argument form. grip_cli.py:1181 records the same split.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parents[1]  # the PYTHONPATH root the suite uses


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(cwd, "git", *args, check=check)


def configure_identity(repo: Path) -> None:
    git(repo, "config", "user.name", "Adoption Test")
    git(repo, "config", "user.email", "adoption@example.test")


def gr2(cwd: Path, *args: str, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(pythonpath or REPO)}
    return subprocess.run(
        [sys.executable, "-m", "gr2.python_cli.app", "store", *args],
        cwd=cwd, text=True, capture_output=True, env=env,
    )


def make_member(tmp_path: Path, name: str) -> Path:
    remote = tmp_path / f"{name}.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    work = tmp_path / f"seed-{name}"
    run(tmp_path, "git", "clone", str(remote), str(work))
    configure_identity(work)
    (work / "README.md").write_text(f"{name}\n")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "initial")
    git(work, "branch", "-M", "main")
    git(work, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def make_adopted_root(tmp_path: Path, name: str, plain: int) -> Path:
    """A root with `plain` commits that PREDATE the store -- the shape that broke `log`."""
    remote = make_member(tmp_path, "alpha")
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-b", "main")
    configure_identity(root)
    for i in range(1, plain + 1):
        git(root, "commit", "--allow-empty", "-m", f"plain commit {i}")
    run(root, "git", "clone", str(remote), "alpha")
    return root


def make_fresh_root(tmp_path: Path, name: str) -> Path:
    """The UNADOPTED control: no history at all before the store."""
    remote = make_member(tmp_path, "beta")
    root = tmp_path / name
    root.mkdir()
    run(root, "git", "clone", str(remote), "beta")
    return root


def test_r1_adopted_root_log_stops_at_the_boundary_and_never_fails_whole(tmp_path: Path) -> None:
    """R1: the measured defect. Exit 0, the store commit shown, the pre-adoption ones not."""
    root = make_adopted_root(tmp_path, "adopted", plain=2)
    assert gr2(root, "init").returncode == 0
    assert gr2(root, "commit", "-m", "first").returncode == 0

    out = gr2(root, "log")
    assert out.returncode == 0, f"log must not fail whole on an adopted root\n{out.stdout}\n{out.stderr}"
    assert "first" in out.stdout, f"the store commit must be shown\n{out.stdout}"
    assert "plain commit" not in out.stdout, (
        f"a commit that predates grip.toml is outside the store's scope and must not be shown\n{out.stdout}"
    )


def test_r2_more_than_one_commit_after_adoption_is_shown(tmp_path: Path) -> None:
    """R2: the specified shape -- 2+ commits, everything after the boundary shown.

    The second commit is earned the way the design requires: a store commit records
    ORIGIN-COVERED pins, so the member's new commit is pushed to its origin first. (A
    no-change `store commit` is a SEPARATE defect, not asserted here: `_native_store_commit`
    runs `git commit` without `--allow-empty`, so it exits 5 "git command failed" on ANY
    root -- adopted or not, measured 2026-09-28. It is filed as its own finding.)
    """
    root = make_adopted_root(tmp_path, "adopted_two", plain=2)
    assert gr2(root, "init").returncode == 0
    assert gr2(root, "commit", "-m", "first").returncode == 0

    work = root / "alpha"
    configure_identity(work)
    (work / "moved.txt").write_text("member moved\n")
    git(work, "add", "moved.txt")
    git(work, "commit", "-m", "member moved")
    git(work, "push", "origin", "main")

    second = gr2(root, "commit", "-m", "second")
    assert second.returncode == 0, f"a second commit must be recordable\n{second.stdout}\n{second.stderr}"

    out = gr2(root, "log")
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "first" in out.stdout and "second" in out.stdout, f"both must be shown\n{out.stdout}"
    assert "plain commit" not in out.stdout, f"{out.stdout}"


def test_r3_unadopted_control_is_unchanged(tmp_path: Path) -> None:
    """R3: a root with no pre-adoption history behaves exactly as it did."""
    root = make_fresh_root(tmp_path, "fresh")
    assert gr2(root, "init").returncode == 0
    assert gr2(root, "commit", "-m", "only").returncode == 0

    out = gr2(root, "log")
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "only" in out.stdout, f"{out.stdout}"


def test_r4_mutation_walking_past_the_boundary_exits_5_again(tmp_path: Path) -> None:
    """R4: THE MUTATION. Drop the boundary skip and exit 5 returns, so R1 measures the fix.

    It mutates the working file IN PLACE under a proven restore -- COMMIT -> MUTATE -> RESTORE
    -> VERIFY BY FRUIT. A copy of the tree was the first attempt and the mutant NEVER RAN:
    this venv carries an editable `gr2`, and an editable install beats PYTHONPATH, so a
    copy on the path is simply not the code that executes. The tell was a mutant that exited
    0 -- the same value the pristine code returns -- which reads as "mutation not caught"
    when the truth is "mutation never loaded". The restore is asserted by fruit (the guard
    count returns to 1), not by trusting the `finally`. The mutation's occurrence count is
    asserted to have MOVED at both ends, counted with str.count and never a regex.

    ⚠ AND THE FIRST MUTATION I WROTE DID NOT BITE EITHER, which read the same way. It
    swapped the boundary probe's `cat-file -e` for `rev-parse --verify`, which is ALSO
    nonzero for a commit without the path -- so the skip still happened and the mutant
    exited 0. A mutation that replaces one working instrument with another working
    instrument measures nothing. This one disables the guard outright.
    """
    guard = 'if _store_git(root, "cat-file", "-e", f"{commit}:grip.toml", check=False).returncode:'
    pristine = REPO / "gr2" / "python_cli" / "grip_cli.py"
    source = pristine.read_text()
    before = source.count(guard)
    assert before == 1, f"the boundary guard must appear exactly once in the pristine source, found {before}"

    root = make_adopted_root(tmp_path, "adopted_mut", plain=2)
    try:
        pristine.write_text(source.replace(guard, "if False:"))
        assert pristine.read_text().count(guard) == 0, "the mutation did not apply"

        assert gr2(root, "init").returncode == 0
        commit = gr2(root, "commit", "-m", "first")
        assert commit.returncode == 0, f"{commit.stdout}\n{commit.stderr}"

        out = gr2(root, "log")
        mutated_exit = out.returncode
        mutated_text = out.stdout + out.stderr
    finally:
        pristine.write_text(source)

    # VERIFY BY FRUIT: the restore actually happened, and the guard is back exactly once
    assert pristine.read_text().count(guard) == before, "the restore did not happen"

    assert mutated_exit == 5, (
        f"MUTATION NOT CAUGHT: with the boundary guard disabled, log must exit 5 again "
        f"(got {mutated_exit})"
    )
    assert "grip.toml" in mutated_text, mutated_text

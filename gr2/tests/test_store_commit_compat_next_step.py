"""A compat `store commit` refused for an unpushed pin names the next step, and only true ones.

THE DEFECT, measured in the run 4 gr2 arm and again on grip dev 7694b7e6: a member with a commit on a branch no
remote holds is refused, exit 3, "... ; push it first". Two operators met it; one "didn't read why", the other went
on without it. The line names no command and no target, and in compat mode it leaves out the one way to keep the
commit unpushed: a full-mode workspace carries it as a layer in the record (measured: rc 0, nothing pushed).

THE RULE: when no remote branch holds the pin, the refusal ends with
  * the push that would cover it, `git -C <member> push -u origin <branch>`, shell-quoted so it runs as printed,
    when the member is on a branch; a detached member is told to push a branch holding the pin;
  * in compat mode only, the full-mode alternative: `mode = "full"` as the first line of the root's grip.toml.
A pin some remote branch already holds keeps its own text (push again would change nothing) and gets no push line.

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, run


def _workspace(tmp_path: Path, dirname: str = "workspace") -> tuple[Path, Path, Path]:
    """One member `alpha` on main, the store initialised and committed (compat: no top-level mode)."""
    seed = tmp_path / "seed"
    run(tmp_path, "git", "init", "-b", "main", str(seed))
    configure_identity(seed)
    (seed / "README.md").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    remote = tmp_path / "alpha.git"
    run(tmp_path, "git", "clone", "--bare", str(seed), str(remote))
    root = tmp_path / dirname
    root.mkdir()
    run(root, "git", "clone", str(remote), "alpha")
    alpha = root / "alpha"
    configure_identity(alpha)
    assert gr2(root, "init").returncode == 0
    gr2(root, "commit", "-m", "base")
    return root, alpha, remote


def _commit(alpha: Path, name: str) -> str:
    (alpha / f"{name}.txt").write_text(f"{name}\n")
    git(alpha, "add", ".")
    git(alpha, "commit", "-m", name)
    return git(alpha, "rev-parse", "HEAD").stdout.strip()


def _root_head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def _refused(root: Path) -> str:
    before = _root_head(root)
    result = gr2(root, "commit", "-m", "unpushed", check=False)
    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert _root_head(root) == before, "a refused store commit moved the root"
    return result.stdout + result.stderr


def _line(said: str, prefix: str) -> str:
    rows = [ln.strip() for ln in said.splitlines() if ln.strip().startswith(prefix)]
    assert len(rows) == 1, f"expected one line starting {prefix!r}:\n{said}"
    return rows[0]


def test_moved_branch_names_the_push_and_full_mode(tmp_path: Path) -> None:
    root, alpha, _ = _workspace(tmp_path)
    git(alpha, "switch", "-c", "feat/F2")
    _commit(alpha, "f2")
    said = _refused(root)
    assert "origin/feat/F2 (it is on feat/F2)" in said, said  # the existing diagnosis is kept
    assert f"git -C {alpha} push -u origin feat/F2" in said, said
    assert 'mode = "full"' in said and str(root / "grip.toml") in said, said


def test_recorded_branch_names_the_push_and_full_mode(tmp_path: Path) -> None:
    root, alpha, _ = _workspace(tmp_path)
    _commit(alpha, "on-main")  # still on its recorded ref, main, ahead of origin/main
    said = _refused(root)
    assert f"git -C {alpha} push -u origin main" in said, said
    assert 'mode = "full"' in said, said


def test_the_printed_push_runs_as_printed_under_a_path_with_a_space(tmp_path: Path) -> None:
    root, alpha, remote = _workspace(tmp_path, "my workspace")
    git(alpha, "switch", "-c", "feat/F2")
    pin = _commit(alpha, "f2")
    said = _refused(root)
    push = _line(said, "push it: ").removeprefix("push it: ")
    ran = subprocess.run(["sh", "-c", push], cwd=tmp_path, text=True, capture_output=True)
    assert ran.returncode == 0, f"{push}\n{ran.stderr}"
    assert git(remote, "rev-parse", "refs/heads/feat/F2").stdout.strip() == pin
    assert gr2(root, "commit", "-m", "now covered", check=False).returncode == 0


def test_the_printed_full_mode_step_records_without_pushing(tmp_path: Path) -> None:
    root, alpha, remote = _workspace(tmp_path)
    git(alpha, "switch", "-c", "feat/F2")
    _commit(alpha, "f2")
    _refused(root)
    toml = root / "grip.toml"
    toml.write_text('mode = "full"\n' + toml.read_text())
    result = gr2(root, "commit", "-m", "carried", check=False)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    heads = git(remote, "for-each-ref", "--format=%(refname)", "refs/heads").stdout.split()
    assert heads == ["refs/heads/main"], heads


def test_a_pin_on_another_remote_branch_gets_no_push_line(tmp_path: Path) -> None:
    root, alpha, _ = _workspace(tmp_path)
    git(alpha, "switch", "-c", "feat/F2")
    _commit(alpha, "f2")
    git(alpha, "push", "origin", "HEAD:refs/heads/elsewhere")
    said = _refused(root)
    assert "origin/elsewhere" in said, said
    assert "push -u" not in said and 'mode = "full"' not in said, said


def test_a_pin_on_another_remote_branch_from_the_recorded_branch_gets_no_push_line(tmp_path: Path) -> None:
    # The member stays on its recorded ref, so this is `_not_on_upstream`'s own "elsewhere" text; the row above
    # moves the member and reaches the moved-branch refusal instead.
    root, alpha, _ = _workspace(tmp_path)
    _commit(alpha, "on-main")
    git(alpha, "push", "origin", "HEAD:refs/heads/elsewhere")
    said = _refused(root)
    assert "origin/elsewhere" in said and "not on origin/main" in said, said
    assert "push -u" not in said and 'mode = "full"' not in said, said


def test_a_detached_member_is_told_to_push_a_branch_holding_the_pin(tmp_path: Path) -> None:
    root, alpha, _ = _workspace(tmp_path)
    pin = _commit(alpha, "detached")
    git(alpha, "switch", "--detach", pin)
    said = _refused(root)
    assert "push -u origin \n" not in said and "push -u origin ''" not in said, said
    assert "push a branch holding" in said and pin in said, said

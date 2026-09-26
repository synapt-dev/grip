"""The smallest real-git proof for the git-native workspace store.

No network and no mocks: each member has a bare local origin, and the root is
created from the gr1 sibling layout rather than a nested workspace fixture.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(cwd, "git", *args, check=check)


def gr2(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    result = subprocess.run(
        [sys.executable, "-m", "gr2.python_cli.app", "store", *args],
        cwd=cwd, text=True, capture_output=True, env=env,
    )
    if check and result.returncode:
        raise AssertionError(f"gr2 store {' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def make_member(tmp_path: Path, name: str) -> tuple[Path, Path]:
    remote = tmp_path / f"{name}.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    work = tmp_path / f"seed-{name}"
    run(tmp_path, "git", "clone", str(remote), str(work))
    git(work, "config", "user.name", "Smoke")
    git(work, "config", "user.email", "smoke@example.test")
    (work / "README.md").write_text(f"{name}\n")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "initial")
    git(work, "branch", "-M", "main")
    git(work, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, work


def test_store_init_refuses_unmarked_root_and_password_remote(tmp_path: Path) -> None:
    remote, _ = make_member(tmp_path, "alpha")
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    run(unmarked, "git", "clone", str(remote), "alpha")
    refused_root = gr2(unmarked, "init", check=False)
    assert refused_root.returncode == 1
    assert "gripspace root" in (refused_root.stdout + refused_root.stderr)
    assert not (unmarked / ".git").exists()

    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / ".gitgrip").mkdir()
    run(marked, "git", "clone", str(remote), "alpha")
    git(marked / "alpha", "remote", "set-url", "origin", "https://token:secret@example.test/alpha.git")
    refused_password = gr2(marked, "init", check=False)
    assert refused_password.returncode == 1
    assert "contains credentials" in (refused_password.stdout + refused_password.stderr)
    assert not (marked / ".git").exists()


def test_store_init_accepts_scp_style_ssh_remote(tmp_path: Path) -> None:
    remote, _ = make_member(tmp_path, "alpha")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitgrip").mkdir()
    run(root, "git", "clone", str(remote), "alpha")
    git(root / "alpha", "remote", "set-url", "origin", "git@github.com:example/alpha.git")
    gr2(root, "init")
    assert (root / ".git").is_dir()
    assert "git@github.com:example/alpha.git" in (root / "grip.toml").read_text()


def test_store_git_native_smoke(tmp_path: Path) -> None:
    alpha_remote, _ = make_member(tmp_path, "alpha")
    beta_remote, _ = make_member(tmp_path, "beta")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitgrip").mkdir()
    run(root, "git", "clone", str(alpha_remote), "alpha")
    run(root, "git", "clone", str(beta_remote), "beta")

    # 1-3: two origins, sibling gr1 layout, then a root store.
    assert not (root / ".git").exists()
    gr2(root, "init")
    assert (root / ".git").is_dir()
    assert (root / "grip.toml").is_file()

    # 4: root commit carries exactly the two member heads as gitlinks.
    gr2(root, "commit", "-m", "first")
    first_root = git(root, "rev-parse", "HEAD").stdout.strip()
    tree = git(root, "ls-tree", "HEAD").stdout
    assert "grip.toml" in tree
    for name in ("alpha", "beta"):
        head = git(root / name, "rev-parse", "HEAD").stdout.strip()
        assert f"160000 commit {head}\t{name}" in tree

    # 5: an unpushed pin is refused and cannot advance the root.
    (root / "alpha" / "next.txt").write_text("unpushed\n")
    git(root / "alpha", "add", "next.txt")
    git(root / "alpha", "commit", "-m", "unpushed")
    unpushed = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    refused = gr2(root, "commit", "-m", "uncovered", check=False)
    assert refused.returncode == 3
    assert "alpha" in (refused.stdout + refused.stderr)
    assert unpushed in (refused.stdout + refused.stderr)
    assert "push it first" in (refused.stdout + refused.stderr).lower()
    assert git(root, "rev-parse", "HEAD").stdout.strip() == first_root

    # 6: once covered, the root gitlink advances.
    git(root / "alpha", "push", "origin", "main")
    gr2(root, "commit", "-m", "second")
    assert git(root, "ls-tree", "HEAD", "alpha").stdout.split()[2] == unpushed

    # 7: a fresh root clone materializes both exact member trees from local origins.
    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "--no-checkout", str(root_remote), str(fresh))
    gr2(fresh, "materialize")
    for name in ("alpha", "beta"):
        assert git(fresh / name, "rev-parse", "HEAD").stdout.strip() == git(root / name, "rev-parse", "HEAD").stdout.strip()
        assert git(fresh / name, "rev-parse", "HEAD^{tree}").stdout.strip() == git(root / name, "rev-parse", "HEAD^{tree}").stdout.strip()

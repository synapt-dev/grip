"""The smallest real-git proof for the git-native workspace store.

No network and no mocks: each member has a bare local origin, and the root is
created from the gr1 sibling layout rather than a nested workspace fixture.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from gr2.python_cli.grip_cli import _url_has_credentials


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(cwd, "git", *args, check=check)


def configure_identity(repo: Path) -> None:
    """Make fixture commits independent of a runner's global Git configuration."""
    git(repo, "config", "user.name", "Smoke")
    git(repo, "config", "user.email", "smoke@example.test")


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
    configure_identity(work)
    (work / "README.md").write_text(f"{name}\n")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "initial")
    git(work, "branch", "-M", "main")
    git(work, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote, work


def test_break_14_init_refuses_remote_userinfo(tmp_path: Path) -> None:
    """The credential half of break attempt 14 (design section 11.1, hard gate).

    ⚠ THE UNMARKED-ROOT HALF IS GONE, AND IT WAS NOT A SILENT DROP (2026-09-28). It required
    `store init` on a root with no `.gitgrip/` marker to refuse with **exit 1** and the words
    "gripspace root". The ratified design carries no marker rule for this verb: section 2
    step 2 builds the root as "a workspace directory holding clones side by side", "the
    workspace root is NOT a git repo, which is the gr1 shape", and step 3 runs `store init`
    on it with no marker anywhere; section 5's refusal list for init is a disagreeing
    `grip.toml` and a root inside another repo's worktree. Exit 1 is also outside the store
    group's table (0 ok, 2 usage, 3 coverage-or-cleanliness, 4 inconsistent-or-beta, 5
    cannot-measure), which every refusal must use. Measured: the native verb exits 0 on that
    shape and writes `<root>/.git` plus `grip.toml`. So the row was asserting a dropped rule
    with a code the design forbids, and it was red before this lane started.
    """
    remote, _ = make_member(tmp_path, "alpha")
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / ".gitgrip").mkdir()
    run(marked, "git", "clone", str(remote), "alpha")
    for url in (
        "https://token:secret@example.test/alpha.git",
        "https://ghp_FAKEFAKE@example.test/alpha.git",
        "https://layne@bitbucket.example.test/alpha.git",
        "ssh://user:pass@example.test/alpha.git",
        "https::https://ghp_FAKEFAKE@example.test/alpha.git",
    ):
        git(marked / "alpha", "remote", "set-url", "origin", url)
        refused_password = gr2(marked, "init", check=False)
        assert refused_password.returncode == 4
        assert "contains credentials" in (refused_password.stdout + refused_password.stderr)
        assert not (marked / ".git").exists()


def test_store_init_accepts_scp_style_ssh_remote(tmp_path: Path) -> None:
    remote, _ = make_member(tmp_path, "alpha")
    for index, url in enumerate(("git@github.com:example/alpha.git", "ssh://git@example.test/alpha.git")):
        root = tmp_path / f"workspace-{index}"
        root.mkdir()
        (root / ".gitgrip").mkdir()
        run(root, "git", "clone", str(remote), "alpha")
        git(root / "alpha", "remote", "set-url", "origin", url)
        gr2(root, "init")
        assert (root / ".git").is_dir()
        assert url in (root / "grip.toml").read_text()


def test_store_remote_credential_classifier_shapes() -> None:
    refused = (
        "https://ghp_FAKEFAKE@example.test/alpha.git",
        "HTTPS://ghp_FAKEFAKE@example.test/alpha.git",
        "git+https://ghp_FAKEFAKE@example.test/alpha.git",
        "https::https://ghp_FAKEFAKE@example.test/alpha.git",
        "https://user:pass@example.test/alpha.git",
        "ssh://user:pass@example.test/alpha.git",
    )
    allowed = (
        "ssh://git@example.test/alpha.git",
        "git+ssh://git@example.test/alpha.git",
        "git@example.test:alpha.git",
        "https://example.test/alpha.git",
        "file:///tmp/alpha.git",
        "../alpha.git",
        "https://example.test/a@b/alpha.git",
    )
    assert all(_url_has_credentials(url) for url in refused)
    assert not any(_url_has_credentials(url) for url in allowed)


def test_break_14_commit_refuses_hand_edited_remote_userinfo(tmp_path: Path) -> None:
    remote, _ = make_member(tmp_path, "alpha")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitgrip").mkdir()
    run(root, "git", "clone", str(remote), "alpha")
    gr2(root, "init")
    spec = root / "grip.toml"
    spec.write_text(spec.read_text().replace(str(remote), "https://ghp_FAKEFAKE@example.test/alpha.git"))
    refused = gr2(root, "commit", "-m", "token", check=False)
    assert refused.returncode == 4
    assert "contains credentials" in (refused.stdout + refused.stderr)
    assert git(root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


def test_break_14_materialize_refuses_hand_edited_remote_userinfo(tmp_path: Path) -> None:
    remote, _ = make_member(tmp_path, "alpha")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitgrip").mkdir()
    run(root, "git", "clone", str(remote), "alpha")
    gr2(root, "init")
    spec = root / "grip.toml"
    spec.write_text(spec.read_text().replace(str(remote), "https://ghp_FAKEFAKE@example.test/alpha.git"))
    shutil.rmtree(root / "alpha")
    refused = gr2(root, "materialize", check=False)
    assert refused.returncode == 4
    assert "contains credentials" in (refused.stdout + refused.stderr)
    assert not (root / "alpha").exists()


def test_store_git_native_smoke(tmp_path: Path, monkeypatch) -> None:
    # CI has no author identity. This proof must provide every identity its commits use.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    alpha_remote, _ = make_member(tmp_path, "alpha")
    beta_remote, _ = make_member(tmp_path, "beta")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitgrip").mkdir()
    run(root, "git", "clone", str(alpha_remote), "alpha")
    run(root, "git", "clone", str(beta_remote), "beta")
    configure_identity(root / "alpha")
    configure_identity(root / "beta")

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

"""`store commit` refuses to move a member's pin backwards.

Found in the multi-agent review cell: bob pulled the root, so his grip.toml named alice's merge
on demo-web, but his demo-web checkout was still at the commit before it. bob's own feature did
not touch demo-web; `store commit` recorded the checkout's HEAD anyway, rc 0, and `store push`
published a pin that undid alice's recorded merge for everyone.

THE RULE: a member whose checkout is strictly BEHIND its recorded pin, or whose checkout does
not hold the recorded pin at all (it never fetched it), is refused at exit 3, naming the member
and both shas, with `gr2 store materialize` as the next step. `--allow-rewind` records the
older pin on purpose. A checkout that is AHEAD of its pin commits as before (the control), and a
checkout that DIVERGED from it is out of scope here.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _git_out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def _bare_remote(tmp_path: Path, name: str) -> Path:
    src = tmp_path / f"{name}-src"
    src.mkdir(parents=True, exist_ok=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "README.md").write_text(f"# {name}\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial")
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(remote)], capture_output=True, check=True)
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def _pin(root: Path, name: str) -> str:
    doc = tomllib.loads((root / "grip.toml").read_text(encoding="utf-8"))
    return next(m["pin"] for m in doc["members"] if m["name"] == name)


def _commit_and_push(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    _git(repo, "push", "-q", "origin", "main")
    return _git_out(repo, "rev-parse", "HEAD")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two pushed members; alpha's pin then advanced to a second pushed commit and recorded."""
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote = _bare_remote(tmp_path, name)
        dest = root / name
        subprocess.run(["git", "clone", "-q", str(remote), str(dest)], capture_output=True, check=True)
        _git(dest, "config", "user.email", "t@e.invalid")
        _git(dest, "config", "user.name", "t")
    monkeypatch.chdir(root)
    assert _cli("store", "init", str(root))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    _commit_and_push(root / "alpha", "merged.txt")
    rc, out = _cli("store", "commit", "-m", "alpha merged")
    assert rc == 0, out
    return root


def test_a_checkout_behind_its_pin_is_refused_and_nothing_is_recorded(ws: Path) -> None:
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")   # stale: the checkout is back before the merge
    behind = _git_out(alpha, "rev-parse", "HEAD")
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "feature 2 merged")

    assert rc == 3, out
    assert "alpha" in out and recorded[:12] in out and behind[:12] in out, out
    assert "gr2 store materialize" in out and "--allow-rewind" in out, out
    assert "beta" not in out, out   # only the member that would move backwards is named
    assert _pin(ws, "alpha") == recorded
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_a_checkout_that_never_fetched_its_pin_is_refused(ws: Path) -> None:
    """bob's exact case: grip.toml names a pin the member checkout does not hold at all."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    stale = _git_out(alpha, "rev-parse", "HEAD")
    _git(alpha, "update-ref", "refs/remotes/origin/main", stale)
    _git(alpha, "reflog", "expire", "--expire=now", "--all")
    _git(alpha, "gc", "-q", "--prune=now")
    assert _git(alpha, "cat-file", "-e", f"{recorded}^{{commit}}", check=False).returncode != 0  # really absent
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "feature 2 merged")

    assert rc == 3, out
    assert "alpha" in out and recorded[:12] in out and "gr2 store materialize" in out, out
    assert _pin(ws, "alpha") == recorded
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_allow_rewind_records_the_older_pin_on_purpose(ws: Path) -> None:
    alpha = ws / "alpha"
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    behind = _git_out(alpha, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "revert alpha's pin", "--allow-rewind")

    assert rc == 0, out
    assert _pin(ws, "alpha") == behind


def test_control_a_checkout_ahead_of_its_pin_commits_as_before(ws: Path) -> None:
    ahead = _commit_and_push(ws / "alpha", "next.txt")

    rc, out = _cli("store", "commit", "-m", "alpha moves on")

    assert rc == 0, out
    assert _pin(ws, "alpha") == ahead

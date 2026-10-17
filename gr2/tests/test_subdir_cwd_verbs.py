"""Store and check verbs run from a subdirectory of a member repo.

A verb that hands git a path taken from the caller's cwd can resolve against the member
repo git discovers, not the root or the repo top level the verb means. Measured from
`<root>/alpha/sub`:
  store checkout HEAD   -> resolved HEAD in alpha (a member commit) where every sibling
                           store verb refuses with "no grip.toml at <cwd>"
  check show .          -> read a snapshot of <cwd> and reported a passing check as absent

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("alpha", "beta"):
        src = tmp_path / f"{name}-src"
        src.mkdir()
        _git(src, "init", "-q", "-b", "main")
        (src / "README.md").write_text(f"# {name}\n")
        _git(src, "add", ".")
        _git(src, "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "-m", "initial")
        _git(tmp_path, "clone", "-q", "--bare", str(src), f"{name}.git")
        _git(tmp_path, "clone", "-q", f"{name}.git", str(root / name))
        _git(root / name, "config", "user.email", "t@e.invalid")
        _git(root / name, "config", "user.name", "t")
    (root / "alpha" / "sub").mkdir()
    (root / "alpha" / "sub" / "f.txt").write_text("x\n")
    _git(root / "alpha", "add", ".")
    _git(root / "alpha", "commit", "-q", "-m", "sub")
    _git(root / "alpha", "push", "-q", "origin", "HEAD:main")
    monkeypatch.chdir(root)
    rc, out = _cli("store", "init", "--json")
    assert rc == 0, out
    rc, out = _cli("store", "commit", "-m", "pins", "--json")
    assert rc == 0, out
    return root


def test_store_checkout_from_a_member_subdirectory_refuses_like_its_siblings(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = ws / "alpha" / "sub"
    alpha_head = _git(ws / "alpha", "rev-parse", "HEAD")
    monkeypatch.chdir(sub)
    rc, out = _cli("store", "status", "--json")
    assert (rc, f"no grip.toml at {sub}" in out) == (5, True), out  # the sibling's refusal, the control
    rc, out = _cli("store", "checkout", "HEAD", "--json")
    assert rc == 5, out
    assert f"no grip.toml at {sub}" in out, out
    assert alpha_head not in out, out  # never resolved against the member repo
    assert _git(ws / "alpha", "rev-parse", "HEAD") == alpha_head


def test_check_run_from_a_member_subdirectory_checks_the_repo(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha_head = _git(ws / "alpha", "rev-parse", "HEAD")
    monkeypatch.chdir(ws / "alpha" / "sub")
    rc, out = _cli("check", "run", ".", "--", "true")
    assert rc == 0, out
    payload = json.loads(out.splitlines()[0])
    assert payload["head"] == alpha_head, out
    assert payload["observation"]["result"] == "pass", out
    rc, out = _cli("check", "show", ".")
    assert rc == 0, out
    assert json.loads(out.splitlines()[0])["status"] == "pass", out

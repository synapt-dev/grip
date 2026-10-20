"""`store add NAME=URL [--ref]` adds one member to an existing store.

WHY THIS EXISTS: `store init --clone` refuses a name the store does not already record
("adding a member to an existing store is not store init") and no other verb added one, so the
workaround was moving grip.toml aside and re-running init with every member.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.test_store_init_clone import _bare_remote, _cli, _git, _members


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    code, out = _cli("store", "init", str(root), "--clone", f"alpha={_bare_remote(tmp_path, 'alpha')}")
    assert code == 0, out
    code, out = _cli("store", "commit", "-C", str(root), "-m", "first")
    assert code == 0, out
    return root


def test_store_add_records_member_pin_and_gitmodules(tmp_path: Path) -> None:
    root = _store(tmp_path)
    beta = _bare_remote(tmp_path, "beta")
    seed = tmp_path / "seed" / "beta"
    _git(seed, "checkout", "-q", "-b", "dev")
    (seed / "dev.txt").write_text("dev\n")
    _git(seed, "add", "dev.txt")
    _git(seed, "commit", "-q", "-m", "dev only")
    _git(seed, "push", "-q", "-u", "origin", "dev")
    dev_tip = _git(seed, "rev-parse", "HEAD")

    code, out = _cli("store", "add", "-C", str(root), f"beta={beta}", "--ref", "dev")
    assert code == 0, out
    members = _members(root)
    assert sorted(members) == ["alpha", "beta"]
    assert members["beta"]["pin"] == dev_tip
    assert members["beta"]["ref"] == "dev"
    assert members["beta"]["remotes"]["origin"] == str(beta)
    assert "path = \"beta\"" in (root / ".gitmodules").read_text()
    assert "beta" in (root / ".gitinclude").read_text().split()

    code, out = _cli("store", "commit", "-C", str(root), "-m", "add beta")
    assert code == 0, out
    tree = _git(root, "ls-tree", "HEAD", "beta").split()
    assert tree[:1] == ["160000"] and tree[2] == dev_tip
    recorded = tomllib.loads(_git(root, "show", "HEAD:grip.toml"))
    assert [m["name"] for m in recorded["members"]] == ["alpha", "beta"]


def test_store_add_refuses_a_duplicate_before_cloning(tmp_path: Path) -> None:
    root = _store(tmp_path)
    before = (root / "grip.toml").read_text()
    other = _bare_remote(tmp_path, "other")
    code, out = _cli("store", "add", "-C", str(root), f"alpha={other}")
    assert code == 4, out
    assert "already a member" in out
    assert (root / "grip.toml").read_text() == before
    assert subprocess.run(["git", "-C", str(root / "alpha"), "remote", "get-url", "origin"],
                          capture_output=True, text=True).stdout.strip() != str(other)



def test_store_add_refusal_names_its_own_argument(tmp_path: Path) -> None:
    # store add takes a positional NAME=URL, not --clone; the refusal names what was typed
    root = _store(tmp_path)
    before = (root / "grip.toml").read_text()
    code, out = _cli("store", "add", "-C", str(root), "no-equals-sign")
    assert code == 2, out
    assert "store add NAME=URL" in out
    assert "is not NAME=URL" in out
    assert "--clone" not in out
    assert (root / "grip.toml").read_text() == before


def test_store_init_clone_refusal_still_names_clone(tmp_path: Path) -> None:
    code, out = _cli("store", "init", str(tmp_path / "ws"), "--clone", "no-equals-sign")
    assert code == 2, out
    assert "cannot use these --clone values" in out
    assert "is not NAME=URL" in out

@pytest.mark.parametrize("step", ["_write_native_members", "render_gitmodules"])
def test_store_add_failed_record_removes_the_clone_and_restores_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    """A failure after the clone (grip.toml just rewritten, or grip.toml already staged) leaves no clone, and the
    record files and the index byte-identical to before the verb ran."""
    from gr2.python_cli import grip_cli

    root = _store(tmp_path)
    (root / ".gitinclude").write_text("alpha\n")
    index = root / _git(root, "rev-parse", "--git-path", "index")
    files = [root / n for n in ("grip.toml", ".gitinclude", ".gitignore", ".gitmodules")] + [index]
    before = {p: (p.read_bytes() if p.exists() else None) for p in files}
    real = getattr(grip_cli, step)

    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError("injected write failure")

    monkeypatch.setattr(grip_cli, step, fail)
    code, out = _cli("store", "add", "-C", str(root), f"beta={_bare_remote(tmp_path, 'beta')}")
    assert code == 5, out
    assert "injected write failure" in out and "beta could not be recorded" in out
    assert not (root / "beta").exists()
    assert {p: (p.read_bytes() if p.exists() else None) for p in files} == before

"""`store init --clone NAME=URL` declares a member from a url in a fresh grip, in the same command.

WHY THIS EXISTS (exam gap 6.1.1, the Show HN eval). The eval starts from bare remotes and no
workspace. `store init` adopted only checkouts that already existed, so the gr2 arm paid one
`git clone` per member plus the init, one command MORE than the Git arm's clones alone, before
any feature work. `--clone` clones each member at its name and records it, so N members cost one
command. The rule: every clone refusal happens before anything is cloned or written where it can
be decided up front, and a clone that fails leaves no `grip.toml` behind, so a rerun finishes the
job (a member already cloned from the same url is adopted, never cloned twice).
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _bare_remote(tmp_path: Path, name: str) -> Path:
    """A local bare remote with one commit on main, the shape the eval starts from."""
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed" / name
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    _git(seed, "checkout", "-q", "-b", "main")
    (seed / "README.md").write_text(f"{name}\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", f"seed {name}")
    _git(seed, "push", "-q", "-u", "origin", "main")
    return bare


def _members(root: Path) -> dict[str, dict]:
    doc = tomllib.loads((root / "grip.toml").read_text())
    return {entry["name"]: entry for entry in doc.get("members", [])}


NAMES = ("alpha", "beta", "gamma", "delta", "epsilon")


def test_five_remotes_one_command_then_commit_and_materialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The smallest proof the story names: five bare remotes, one init, no file edited by hand."""
    remotes = {name: _bare_remote(tmp_path, name) for name in NAMES}
    root = tmp_path / "work"   # does not exist yet: a fresh grip
    args = ["store", "init", str(root)]
    for name in NAMES:
        args += ["--clone", f"{name}={remotes[name]}"]

    rc, out = _cli(*args)
    assert rc == 0, out
    members = _members(root)
    assert sorted(members) == sorted(NAMES), out
    for name in NAMES:
        assert members[name]["path"] == name
        assert members[name]["remotes"]["origin"] == str(remotes[name])
        assert members[name]["ref"] == "main" and members[name]["upstream"] == "origin/main"
        assert members[name]["pin"] == _git(remotes[name], "rev-parse", "main")

    # The record it made is a working store: commit, then a fresh copy materializes every member.
    monkeypatch.chdir(root)
    rc, out = _cli("store", "commit", "-m", "five members")
    assert rc == 0, out
    copy = tmp_path / "copy"
    subprocess.run(["git", "clone", "-q", str(root), str(copy)], check=True, capture_output=True)
    monkeypatch.chdir(copy)
    rc, out = _cli("store", "materialize")
    assert rc == 0, out
    for name in NAMES:
        assert _git(copy / name, "rev-parse", "HEAD") == members[name]["pin"]


def test_a_relative_local_url_is_recorded_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative path recorded as an origin would resolve against whichever directory a later clone runs in."""
    _bare_remote(tmp_path, "alpha")
    monkeypatch.chdir(tmp_path)
    rc, out = _cli("store", "init", "work", "--clone", "alpha=remotes/alpha.git")
    assert rc == 0, out
    origin = _members(tmp_path / "work")["alpha"]["remotes"]["origin"]
    assert origin == str((tmp_path / "remotes" / "alpha.git").resolve()), origin


def test_clone_mixes_with_an_existing_member_path(tmp_path: Path) -> None:
    """--clone adds to the named members; a checkout already in place is named with --member as before."""
    alpha = _bare_remote(tmp_path, "alpha")
    beta = _bare_remote(tmp_path, "beta")
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(beta), str(root / "beta")], check=True, capture_output=True)

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}", "--member", "beta")
    assert rc == 0, out
    assert sorted(_members(root)) == ["alpha", "beta"], out


def test_a_failed_clone_writes_no_store_and_a_rerun_finishes(tmp_path: Path) -> None:
    """A clone that fails names the member and leaves no grip.toml; the rerun adopts what was cloned."""
    alpha = _bare_remote(tmp_path, "alpha")
    missing = tmp_path / "remotes" / "nowhere.git"
    root = tmp_path / "work"

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}", "--clone", f"beta={missing}")
    assert rc == 5, out
    assert "beta" in out and "done: alpha" in out, out
    assert not (root / "grip.toml").exists()
    assert not (root / ".git").exists()

    # The rerun, with the url fixed: alpha is adopted (its untracked file survives), beta is cloned.
    (root / "alpha" / "untracked.txt").write_text("kept\n")
    beta = _bare_remote(tmp_path, "beta")
    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}", "--clone", f"beta={beta}")
    assert rc == 0, out
    assert (root / "alpha" / "untracked.txt").read_text() == "kept\n"
    assert sorted(_members(root)) == ["alpha", "beta"], out


def test_a_checkout_at_the_name_with_another_origin_refuses_before_cloning(tmp_path: Path) -> None:
    alpha = _bare_remote(tmp_path, "alpha")
    other = _bare_remote(tmp_path, "other")
    beta = _bare_remote(tmp_path, "beta")
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(other), str(root / "alpha")], check=True, capture_output=True)

    rc, out = _cli("store", "init", str(root), "--clone", f"beta={beta}", "--clone", f"alpha={alpha}")
    assert rc == 4, out
    assert "alpha" in out and "origin" in out, out
    assert not (root / "beta").exists(), "a refusal decidable up front must come before any clone"
    assert not (root / "grip.toml").exists()


def test_a_non_empty_folder_at_the_name_refuses(tmp_path: Path) -> None:
    alpha = _bare_remote(tmp_path, "alpha")
    root = tmp_path / "work"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "notes.txt").write_text("mine\n")

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}")
    assert rc == 4, out
    assert (root / "alpha" / "notes.txt").read_text() == "mine\n"
    assert not (root / "grip.toml").exists()


def test_a_url_with_credentials_refuses_without_printing_it(tmp_path: Path) -> None:
    root = tmp_path / "work"
    rc, out = _cli("store", "init", str(root), "--clone", "alpha=https://user:s3cret@example.invalid/alpha.git")
    assert rc == 4, out
    assert "s3cret" not in out, out
    assert not root.exists() or not any(root.iterdir())


@pytest.mark.parametrize("value", ["alpha", "=url", "a/b=url", "..=url", ".hidden=url", "-x=url", "alpha="])
def test_a_malformed_clone_value_is_a_usage_error(tmp_path: Path, value: str) -> None:
    root = tmp_path / "work"
    rc, out = _cli("store", "init", str(root), "--clone", value)
    assert rc == 2, out
    assert not root.exists()


def test_the_same_name_twice_is_a_usage_error(tmp_path: Path) -> None:
    alpha = _bare_remote(tmp_path, "alpha")
    root = tmp_path / "work"
    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}", "--clone", f"alpha={alpha}")
    assert rc == 2, out
    assert not root.exists()


def test_rerun_on_an_existing_store_is_a_noop_and_a_new_name_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a member to a store that already exists is not this verb: it refuses before cloning."""
    alpha = _bare_remote(tmp_path, "alpha")
    beta = _bare_remote(tmp_path, "beta")
    root = tmp_path / "work"
    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}")
    assert rc == 0, out

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}")
    assert rc == 0, out

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}", "--clone", f"beta={beta}")
    assert rc == 4, out
    assert "beta" in out, out
    assert not (root / "beta").exists()
    assert sorted(_members(root)) == ["alpha"]

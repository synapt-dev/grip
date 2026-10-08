"""`store init` member-path hygiene: three follow-ons from the reads on the range that added
the member-path checks.

All three were found by readers on that range and ruled non-blocking there, because
none is a regression: two are the base's behaviour and one is dead code the range left behind.

WHAT EACH ROW PINS
  1. A member path that RESOLVES to the root, or to a path another member already holds
     (a symlink `self -> .` or `alias -> <member>`) is accepted by init today, exactly as the
     base accepts it, and `store commit` then refuses it at 4 by name -- so init builds a store
     whose commit is refused. The refusal moves to init.
  2. An 8-line UNREACHABLE copy of the direct-child discovery loop sat after the new `return`
     (gr2/gr2/python_cli/grip_cli.py:572). Dead code is asserted against behaviourally: the verb's
     own rows pass either way, so the row here pins the SHAPE -- the loop appears once.
  3. A member's NAME is taken from the caller's raw spelling rather than the normalised path,
     so `./core/config` named a member `.-core-config` before normalisation ran. Normalisation
     now runs first, so the dotted case is gone, and this row pins the general rule.
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


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _member_clone(root: Path, rel: str, bare: Path) -> Path:
    bare.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    clone = root / rel
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, capture_output=True, text=True)
    _git(clone, "checkout", "-q", "-b", "main")
    (clone / "README.md").write_text(f"{rel}\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", f"member {rel}")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return clone


def _store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _members_of(root: Path) -> dict[str, str]:
    doc = tomllib.loads((root / "grip.toml").read_text())
    return {entry["name"]: entry["path"] for entry in doc.get("members", [])}


def test_a_member_path_that_resolves_to_the_root_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`self -> .` must refuse at init, not build a store whose commit is refused later."""
    root = _store_root(tmp_path, monkeypatch)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "https://example.invalid/root.git")
    (root / "self").symlink_to(root, target_is_directory=True)

    rc, out = _cli("store", "init", str(root), "--member", "self")
    assert rc == 4, f"a path resolving to the root must refuse at 4, got {rc}: {out}"
    assert "self" in out, f"the refusal must name the path, got: {out}"
    assert not (root / "grip.toml").exists(), "a refused init must not leave a spec behind"


def test_a_member_path_that_aliases_another_member_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`alias -> core/config` is one checkout under two names, and must refuse."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "core/config", tmp_path / "bare-core-config")
    (root / "alias").symlink_to(root / "core" / "config", target_is_directory=True)

    rc, out = _cli("store", "init", str(root), "--member", "core/config", "--member", "alias")
    assert rc == 4, f"an aliased member path must refuse at 4, got {rc}: {out}"
    assert "alias" in out, f"the refusal must name the aliasing path, got: {out}"


def test_the_member_name_comes_from_the_NORMALISED_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`./core/config` and `core/config` name the same member the same way."""
    root = _store_root(tmp_path, monkeypatch)
    _member_clone(root, "core/config", tmp_path / "bare-core-config")

    rc, out = _cli("store", "init", str(root), "--member", "./core/config")
    assert rc == 0, out
    assert _members_of(root) == {"core-config": "core/config"}, (
        f"the name must come from the normalised path, not the caller's spelling: {out}"
    )


def test_the_direct_child_discovery_loop_appears_once() -> None:
    """A SHAPE row, and it says so: the 8-line unreachable copy must not come back.

    This is the one follow-on that cannot be witnessed behaviourally -- unreachable code by
    definition changes nothing a caller can see -- so the row pins the shape instead: exactly
    one `for path in sorted(root.iterdir())` loop in the module that owns store init. It is a
    source assertion on purpose, and it fails if a future edit reintroduces a second copy.
    """
    source = (Path(__file__).resolve().parents[1] / "gr2" / "python_cli" / "grip_cli.py").read_text()
    loop = "for path in sorted(root.iterdir()):"
    assert source.count(loop) == 1, (
        f"the direct-child discovery loop appears {source.count(loop)} times; "
        "one copy is the live one and a second is the dead residue this row exists to prevent"
    )

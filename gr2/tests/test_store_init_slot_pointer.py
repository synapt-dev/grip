"""`store init` writes `<root>/.gr2-slot` from GR2_SLOT, and only from it.

WHY THIS EXISTS. A launcher may pick which gr2 build runs in a root by reading a
one-line pointer file there. A root made by `store init` had none, so a launcher that resolved a
build for the caller lost it in the new root. The rule: GR2_SLOT set to a slot name -> the file
holds exactly that name; unset -> no file, as before; a value that is not one plain entry (a
letter or digit, then letters, digits, '.', '_' or '-', no trailing '.' or '..', at most 64) -> no
file and one stderr line naming the variable; an existing file is never touched and a symlink at the pointer is never written
through, even when it appears between a check and the write (a second init on the same root).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner
from tests.test_store_declared_members import _member_clone


def _init(root: Path, env: dict[str, str | None]) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, ["store", "init", "--root", str(root)], env=env)
    return result.exit_code, result.stderr or ""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    _member_clone(root, "app", tmp_path / "remotes" / "app.git")
    return root


def test_set_writes_exact_value(root: Path) -> None:
    code, err = _init(root, {"GR2_SLOT": "dev-7692e31"})
    assert code == 0, err
    assert (root / ".gr2-slot").read_text() == "dev-7692e31\n"
    assert "GR2_SLOT" not in err


def test_unset_writes_nothing(root: Path) -> None:
    code, err = _init(root, {"GR2_SLOT": None})
    assert code == 0, err
    assert not (root / ".gr2-slot").exists()
    assert "GR2_SLOT" not in err


@pytest.mark.parametrize(
    "value",
    [
        "", "a/b", "..", "dev..x", "/abs", "a\nb", ".", "a\rb", "a\tb", "a\\b", " ", " dev ", "dev\x7f",
        "dev\x0bx", "-rf", ".hidden", "a b", "dev.", "dev-x\n", "d\u00e9v", "a" * 65,
    ],
)
def test_invalid_writes_nothing_and_names_variable(root: Path, value: str) -> None:
    code, err = _init(root, {"GR2_SLOT": value})
    assert code == 0, err
    assert not (root / ".gr2-slot").exists()
    lines = [line for line in err.splitlines() if "GR2_SLOT" in line]
    assert len(lines) == 1, err


@pytest.mark.parametrize("value", ["dev-7692e31", "dev-1.2", "R_2", "9", "a" * 64])
def test_plain_entry_names_are_written(root: Path, value: str) -> None:
    code, err = _init(root, {"GR2_SLOT": value})
    assert code == 0, err
    assert (root / ".gr2-slot").read_text() == value + "\n"


def test_dangling_symlink_not_followed(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"
    (root / ".gr2-slot").symlink_to(target)
    code, err = _init(root, {"GR2_SLOT": "x"})
    assert code == 0, err
    assert not target.exists()
    assert (root / ".gr2-slot").is_symlink()


def test_existing_file_untouched(root: Path) -> None:
    (root / ".gr2-slot").write_text("released-1\n")
    code, err = _init(root, {"GR2_SLOT": "dev-7692e31"})
    assert code == 0, err
    assert (root / ".gr2-slot").read_text() == "released-1\n"


def test_written_pointer_is_not_root_content(root: Path) -> None:
    """The pointer is per-checkout; the root's own git must not see it as a change to record."""
    code, err = _init(root, {"GR2_SLOT": "dev-7692e31"})
    assert code == 0, err
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", ".gr2-slot"],
                            capture_output=True, text=True, check=True).stdout
    assert status == "", status


def _blind_to_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any exists()/is_symlink() check on the pointer report absent, as it does for a writer
    whose check ran before a racing init (or a planted link) put the entry there."""
    for name in ("exists", "is_symlink", "is_file", "lexists"):
        real = getattr(Path, name, None)
        if real is None:
            continue
        monkeypatch.setattr(Path, name, lambda self, *a, _real=real, **k: False
                            if self.name == ".gr2-slot" else _real(self, *a, **k))


def test_race_existing_file_untouched(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A racing init wrote its pointer after this one looked: the first writer's file stays."""
    (root / ".gr2-slot").write_text("released-1\n")
    _blind_to_slot(monkeypatch)
    code, err = _init(root, {"GR2_SLOT": "dev-7692e31"})
    assert code == 0, err
    assert (root / ".gr2-slot").read_text() == "released-1\n"


def test_race_planted_link_not_followed(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dangling link planted after the look is not written through."""
    target = tmp_path / "target"
    (root / ".gr2-slot").symlink_to(target)
    _blind_to_slot(monkeypatch)
    code, err = _init(root, {"GR2_SLOT": "evil"})
    monkeypatch.undo()
    assert code == 0, err
    assert not target.exists()
    assert (root / ".gr2-slot").is_symlink()

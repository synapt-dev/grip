"""A member's consent key is a plain workspace-relative path, read under its exact name.

THE RULE: a consent key with a leading "/", or an empty, "." or ".." part, is refused, never normalized, and
a record is read only when every component of its path is listed under exactly that name. `hooks trust`
resolves its member argument inside the workspace and refuses one that is not a plain relative path or
resolves outside it, so no record is written elsewhere.

Fixtures are real directories under tmp_path, with the hook text the existing consent tests use.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli import consent as consent_mod
from gr2.python_cli.consent import (
    PENDING_DIR,
    _exact_name_exists,
    consent_state,
    pending_marker_path,
    plain_relative_key,
    take_pending_marker,
    write_consent,
    write_pending_marker,
)
from gr2.python_cli.hooks import HookContext, run_lifecycle_stage
from tests.test_hooks_consent import HOOKS_TOML, _make_hooks


def _member(path: Path, text: str = HOOKS_TOML) -> Path:
    (path / ".gr2").mkdir(parents=True, exist_ok=True)
    (path / ".gr2" / "hooks.toml").write_text(text)
    return path


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _ctx(ws: Path, name: str) -> HookContext:
    return HookContext(
        workspace_root=ws, unit_root=ws / "default", lane_root=ws / "lanes" / "default" / "feat",
        repo_root=ws / name, repo_name=name, lane_owner="default", lane_subject=name, lane_name="feat",
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """repos/a trusted, and a nested member x/y trusted, so the record directory .grip/consent/x exists."""
    root = tmp_path / "ws"
    root.mkdir()
    write_consent(root, "repos/a", _member(root / "repos" / "a"))
    write_consent(root, "x/y", _member(root / "x" / "y", "[[lifecycle.on_enter]]\nname = 'y'\ncommand = 'true'\n"))
    return root


@pytest.mark.parametrize("key", ["x/../repos/a", "../x", "/abs/path", "a//b", "./a", "a/.", ""])
def test_a_key_that_is_not_a_plain_relative_path_is_refused(key: str) -> None:
    with pytest.raises(ValueError):
        plain_relative_key(key)


def test_a_key_with_a_dotdot_part_reads_invalid(ws: Path) -> None:
    other = _member(ws / "other")   # same hooks.toml text as repos/a
    assert consent_state(ws, "repos/a", ws / "repos" / "a")[0] == "bound"   # control: the plain key binds
    assert consent_state(ws, "x/../repos/a", other)[0] == "invalid"


def _spec_declares(ws: Path, name: str, path: str) -> None:
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'workspace_name = "ws"\n\n[[repos]]\nname = "{name}"\npath = "{path}"\nurl = "unused"\n'
    )


def test_a_spec_declared_path_with_a_dotdot_part_does_not_run_the_hook(ws: Path) -> None:
    """The workspace spec declares member other at a path with a ".." part: its key reads invalid."""
    other = _member(ws / "other")
    _spec_declares(ws, "other", "x/../repos/a")
    before = _files(ws)

    run_lifecycle_stage(_make_hooks(), "on_enter", _ctx(ws, "other"), repo_dirty=False, first_materialize=False)

    assert not (other / "marker").exists()
    # no consent record and no pending marker written anywhere (the event log of the skip is expected)
    assert {f for f in _files(ws) if not f.startswith(".grip/events")} == before


@pytest.mark.parametrize("arg", ["../outside", "../deep/outside", "ABSOLUTE"])
def test_trust_refuses_a_member_outside_the_workspace_and_writes_nothing(ws: Path, tmp_path: Path, arg: str) -> None:
    outside = _member(tmp_path / "outside")
    _member(tmp_path / "deep" / "outside")
    if arg == "ABSOLUTE":
        arg = str(outside)
    before = _files(tmp_path)

    result = CliRunner().invoke(app, ["hooks", "trust", arg, "--workspace-root", str(ws)])

    assert result.exit_code != 0, result.output
    assert "bound:" not in result.output
    assert _files(tmp_path) == before


def test_control_trust_binds_a_member_inside_the_workspace(ws: Path) -> None:
    member = _member(ws / "repo-a")

    result = CliRunner().invoke(app, ["hooks", "trust", "repo-a", "--workspace-root", str(ws)])

    assert result.exit_code == 0, result.output
    assert (ws / ".grip" / "consent" / "repo-a.json").exists()
    assert consent_state(ws, "repo-a", member)[0] == "bound"


def test_trust_refuses_a_member_that_is_a_symlink_to_a_directory_outside(ws: Path, tmp_path: Path) -> None:
    """A plain name can still resolve outside: a member directory that is a symlink out of the workspace."""
    _member(tmp_path / "elsewhere")
    (ws / "linked").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    before = _files(tmp_path)

    result = CliRunner().invoke(app, ["hooks", "trust", "linked", "--workspace-root", str(ws)])

    assert result.exit_code != 0, result.output
    assert "outside the workspace" in result.output
    assert _files(tmp_path) == before


def test_a_record_is_read_only_under_its_exact_name(ws: Path) -> None:
    base = ws / ".grip" / "consent"
    assert _exact_name_exists(base, base / "repos" / "a.json")   # control
    assert not _exact_name_exists(base, base / "REPOS" / "a.json")
    assert not _exact_name_exists(base, base / "repos" / "A.json")


def _filesystem_matches(tmp_path: Path, written: str, looked_up: str) -> bool:
    (tmp_path / "probe").mkdir(exist_ok=True)
    (tmp_path / "probe" / written).write_text("")
    return (tmp_path / "probe" / looked_up).exists()


@pytest.mark.parametrize("spelled", ["REPOS/A", "NFD"])
def test_a_differently_spelled_spec_path_does_not_run_the_hook(ws: Path, tmp_path: Path, spelled: str) -> None:
    """Only meaningful where the filesystem matches names loosely, so it skips (and says so) elsewhere."""
    if spelled == "NFD":
        nfc = unicodedata.normalize("NFC", "caf\u00e9")
        if not _filesystem_matches(tmp_path, nfc, unicodedata.normalize("NFD", nfc)):
            pytest.skip("this filesystem distinguishes Unicode normalization forms")
        write_consent(ws, f"repos/{nfc}", _member(ws / "repos" / nfc))
        spelled = f"repos/{unicodedata.normalize('NFD', nfc)}"
    elif not _filesystem_matches(tmp_path, "Probe", "PROBE"):
        pytest.skip("this filesystem is case-sensitive")
    other = _member(ws / "other")
    _spec_declares(ws, "other", spelled)

    run_lifecycle_stage(_make_hooks(), "on_enter", _ctx(ws, "other"), repo_dirty=False, first_materialize=False)

    assert not (other / "marker").exists()


def test_taking_a_pending_marker_consults_the_exact_name_check(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs on every filesystem: with the exact-name check answering no, a marker that exists is not taken, and the
    check was asked about that marker under the pending store."""
    write_pending_marker(ws, "repos/a")
    path = pending_marker_path(ws, "repos/a")
    asked = []

    def no(base: Path, target: Path) -> bool:
        asked.append((base, target))
        return False

    monkeypatch.setattr(consent_mod, "_exact_name_exists", no)
    assert take_pending_marker(ws, "repos/a") is False
    assert path.exists()
    assert asked == [(ws / PENDING_DIR, path)]
    monkeypatch.undo()
    assert take_pending_marker(ws, "repos/a") is True   # control: the real check finds it under its exact name
    assert not path.exists()


def _aliased_key(tmp_path: Path, ws: Path, spelled: str) -> tuple[str, str]:
    """(the key a marker is written under, a differently spelled key for the same name); skips where they stay distinct."""
    if spelled == "NFD":
        nfc = unicodedata.normalize("NFC", "caf\u00e9")
        if not _filesystem_matches(tmp_path, nfc, unicodedata.normalize("NFD", nfc)):
            pytest.skip("this filesystem distinguishes Unicode normalization forms")
        return f"repos/{nfc}", f"repos/{unicodedata.normalize('NFD', nfc)}"
    if not _filesystem_matches(tmp_path, "Probe", "PROBE"):
        pytest.skip("this filesystem is case-sensitive")
    return "repos/a", "REPOS/A"


@pytest.mark.parametrize("spelled", ["case", "NFD"])
def test_a_differently_spelled_key_does_not_take_another_members_marker(ws: Path, tmp_path: Path, spelled: str) -> None:
    """Only meaningful where the filesystem matches names loosely, so it skips (and says so) elsewhere."""
    key, alias = _aliased_key(tmp_path, ws, spelled)
    write_pending_marker(ws, key)
    path = pending_marker_path(ws, key)
    before = path.read_bytes()

    assert take_pending_marker(ws, alias) is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("spelled", ["case", "NFD"])
def test_a_differently_spelled_key_never_overwrites_another_members_marker(ws: Path, tmp_path: Path, spelled: str) -> None:
    """Only meaningful where the filesystem matches names loosely, so it skips (and says so) elsewhere."""
    key, alias = _aliased_key(tmp_path, ws, spelled)
    write_pending_marker(ws, key)
    path = pending_marker_path(ws, key)
    before = path.read_bytes()

    assert write_pending_marker(ws, alias) is False
    assert path.read_bytes() == before

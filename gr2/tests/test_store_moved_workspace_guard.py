"""A b1 `store` verb that meets a workspace whose state has MOVED refuses in one sentence.

Without it, an older gr2 answered a moved workspace (no `grip.toml`, no gitlinks at HEAD) with "run
store init", and following that advice started a second, divergent workspace beside the real one.
A moved workspace is either a root whose HEAD tracks the marker file, or a root that holds the state
ref; a plain root, and a root with an UNTRACKED marker, are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import grip_cli
from gr2.python_cli.layout import MOVED_MARKER

from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401

VERBS = [
    ("init",), ("commit", "-m", "x"), ("check",), ("push",), ("status",), ("materialize",),
    ("migrate",), ("log",), ("diff", "HEAD", "HEAD"), ("checkout", "HEAD"),
]


def _commit(ws: Path, *paths: str) -> None:
    _git(ws, "add", "-f", *paths)
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "moved")


def _store_root(ws: Path) -> Path:
    assert _cli("store", "init", str(ws))[0] == 0
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "add", "-A")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "first")
    return ws


def _tombstoned(ws: Path) -> Path:
    """A workspace after a move: the marker tracked at HEAD, grip.toml untracked and gone."""
    _store_root(ws)
    (ws / MOVED_MARKER).write_text("state moved elsewhere\n")
    _git(ws, "rm", "-q", "--cached", "grip.toml")
    (ws / "grip.toml").unlink()
    _commit(ws, MOVED_MARKER)
    return ws


@pytest.mark.parametrize("verb", VERBS, ids=[v[0] for v in VERBS])
def test_every_store_verb_refuses_a_moved_workspace_in_one_sentence(two_member_ws, monkeypatch, verb) -> None:
    ws = _tombstoned(two_member_ws)
    monkeypatch.chdir(ws)
    code, out = _cli("store", *verb)
    assert code == 4, (verb, code, out)
    text = out.strip()
    assert "moved" in text and len(text.splitlines()) == 1, text
    assert "store init" not in text and "run" not in text.lower().split(), text
    assert not (ws / "grip.toml").exists(), "a verb recreated grip.toml beside the moved state"


def test_the_state_ref_alone_marks_a_moved_workspace(two_member_ws, monkeypatch) -> None:
    ws = two_member_ws
    _store_root(ws)
    _git(ws, "update-ref", grip_cli.MOVED_STATE_REF, _git_out(ws, "rev-parse", "HEAD"))
    monkeypatch.chdir(ws)
    code, out = _cli("store", "status")
    assert code == 4 and "moved" in out, out


def test_a_plain_store_root_is_not_refused(two_member_ws, monkeypatch) -> None:
    ws = two_member_ws
    _store_root(ws)
    monkeypatch.chdir(ws)
    code, out = _cli("store", "status")
    assert code == 0 and "moved" not in out, out
    assert _cli("store", "init", str(ws))[0] == 0, "init on a plain root stays idempotent"


def test_an_untracked_marker_file_does_not_make_a_moved_workspace(two_member_ws, monkeypatch) -> None:
    ws = two_member_ws
    _store_root(ws)
    (ws / MOVED_MARKER).write_text("a stray copy, not committed\n")
    monkeypatch.chdir(ws)
    code, out = _cli("store", "status")
    assert code == 0 and "moved" not in out, out

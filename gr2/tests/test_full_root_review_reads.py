"""`review list` and `review show` at a full-mode root refuse with the full-mode sentence after `workspace init` too.

A full-mode root keeps its pins in its own .git, and these two verbs do not read it yet; before `workspace init` they
say so. `workspace init` writes `.grip/workspace_spec.toml`, and the read then fell through to "No review is pinned",
which is false while a pin's ref is there. A spec workspace that is not a full root keeps that refusal and writes
nothing. Each row names what must make it go red:
  * the full-root check before the spec workspace's refusal is dropped -> the after-init row
  * the spec workspace's refusal is dropped (a read sets up a store)   -> the spec-workspace row
  * the refusal is a GripInitError, not a ReviewStoreAbsent            -> the callers row
  * the CLI maps it to not_bound, not the full-mode sentence           -> the receive row and the after-init row
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import app as app_mod, grip_cli
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner
from tests.test_review_pin_full_root import _checkout, _reviews

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _run(where: Path, monkeypatch: pytest.MonkeyPatch, *argv: str):
    monkeypatch.chdir(where)
    r = make_cli_runner().invoke(app, list(argv))
    out = " ".join(re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))).split())
    return r.exit_code, out


def _sentence(root: Path) -> str:
    return (f"{root} is a full-mode gr2 root (no grip.toml; members under <member>.griplink), and this "
            "verb does not read a full-mode root yet; `gr2 status` reads it")


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A full root with one member `app` ahead of its gitlink and pinned: the pin's ref is in the root's .git."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "app-src"
    _git("init", "-q", "-b", "main", str(src))
    (src / "a.txt").write_text("v1\n")
    _git("-C", str(src), "add", "a.txt")
    _git("-C", str(src), "commit", "-q", "-m", "base")
    bare = tmp_path / "app.git"
    _git("clone", "-q", "--bare", str(src), str(bare))
    rc, out = _run(tmp_path, monkeypatch, "store", "init", "--mode", "full", "--clone", f"app=file://{bare}", "root")
    assert rc == 0, out
    root = (tmp_path / "root").resolve()
    (_checkout(root, "app") / "a.txt").write_text("v2\n")
    _git("-C", str(_checkout(root, "app")), "commit", "-q", "-am", "app v2")
    rc, out = _run(root, monkeypatch, "review", "pin")
    assert rc == 0, out
    reviews = _reviews(root)
    assert len(reviews) == 1, reviews  # control: the pin is there to be read
    return root, reviews[0].rsplit("/", 1)[1]


def _reads(rid: str) -> list[tuple[str, ...]]:
    return [("review", "list"), ("review", "show"), ("review", "show", rid)]


def test_full_root_reads_before_workspace_init_refuse_with_the_full_mode_sentence(pinned, monkeypatch):
    root, rid = pinned
    assert not (root / ".grip").exists()
    for argv in _reads(rid):
        rc, out = _run(root, monkeypatch, *argv)
        assert rc == 2, (argv, out)
        assert "not_initialized" in out and _sentence(root) in out, (argv, out)
        assert "not_bound" not in out and "No review is pinned" not in out, (argv, out)


def test_full_root_reads_after_workspace_init_refuse_as_before_never_not_bound(pinned, monkeypatch):
    root, rid = pinned
    before = {argv: _run(root, monkeypatch, *argv) for argv in _reads(rid)}
    rc, out = _run(root, monkeypatch, "workspace", "init")
    assert rc == 0, out
    assert (root / ".grip" / "workspace_spec.toml").is_file()  # control: the spec that turned the refusal false
    for argv in _reads(rid):
        rc, out = _run(root, monkeypatch, *argv)
        assert rc == before[argv][0] == 2, (argv, out)
        assert "not_initialized" in out and _sentence(root) in out, (argv, out)
        assert "not_bound" not in out and "No review is pinned" not in out, (argv, out)
    assert _reviews(root) == [f"refs/dev.synapt.grip/__reviews__/v1/{rid}"]


def test_spec_workspace_with_no_store_still_refuses_not_bound_and_writes_nothing(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "app-src"
    _git("init", "-q", "-b", "main", str(src))
    (src / "a.txt").write_text("v1\n")
    _git("-C", str(src), "add", "a.txt")
    _git("-C", str(src), "commit", "-q", "-m", "base")
    ws = tmp_path / "ws"
    _git("clone", "-q", str(src), str(ws / "app"))
    rc, out = _run(ws, monkeypatch, "workspace", "init")
    assert rc == 0, out
    ws = ws.resolve()
    listing = (sorted(os.listdir(ws)), sorted(os.listdir(ws / ".grip")))
    assert listing[0] == [".grip", "app"], listing  # control: a spec workspace, no root .git, no grip.toml
    for argv in (("review", "list"), ("review", "show")):
        rc, out = _run(ws, monkeypatch, *argv)
        assert rc == 2, (argv, out)
        assert f"not_bound: No review is pinned in {ws}: nothing has been stored there yet." in out, (argv, out)
        assert "full-mode" not in out, (argv, out)
        assert (sorted(os.listdir(ws)), sorted(os.listdir(ws / ".grip"))) == listing, (argv, listing)


def test_full_root_after_init_callers_that_read_nothing_bound_as_empty_go_on_as_before(pinned, monkeypatch):
    """`pr create --repo-path` (`_pin_holding_exactly`) and `store commit` (`_member_records`) read "nothing bound"
    as an empty state and go on; `review stamp` names it not_received. Each keeps that, never a GripInitError."""
    root, rid = pinned
    rc, out = _run(root, monkeypatch, "workspace", "init")
    assert rc == 0, out
    assert app_mod._pin_holding_exactly(root, {}) is None
    pin = _git("-C", str(_checkout(root, "app")), "rev-parse", "HEAD")
    assert grip_cli._member_records(root, {"path": str(_checkout(root, "app").relative_to(root)), "pin": pin}) == []
    monkeypatch.setenv("GIT_AUTHOR_NAME", "y")  # not the author, so self_approval is not what refuses
    rc, out = _run(root, monkeypatch, "review", "stamp", str(root), f"gr:{rid}")
    assert rc == 2, out
    assert f"refused: not_received: {_sentence(root)}" in out, out


def test_full_root_receive_after_workspace_init_refuses_with_the_full_mode_sentence(pinned, monkeypatch, tmp_path):
    root, rid = pinned
    rc, out = _run(root, monkeypatch, "workspace", "init")
    assert rc == 0, out
    rc, out = _run(root, monkeypatch, "review", "receive", str(root), f"gr:{rid}", "--remote", str(tmp_path / "app.git"))
    assert rc == 2, out
    assert "not_initialized" in out and _sentence(root) in out, out
    assert "not_bound" not in out and "No review is pinned" not in out, out

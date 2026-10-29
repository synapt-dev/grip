"""A full-mode root that `workspace init` was also run at (so it carries `.grip/workspace_spec.toml`) is still a full-mode
root: the tree decides, not the spec file. A grip.toml still makes a root native, and a spec workspace with no full-mode
tree is still not a full root."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import declared_sets, events
from gr2.python_cli.app import app
from gr2.python_cli.declared_sets import DeclaredSetError
from gr2.python_cli.events import EventType
from gr2.python_cli.workspace_guidance import is_full_mode_root

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _invoke(cwd: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(cwd)
    return make_cli_runner().invoke(app, list(args))


@pytest.fixture
def adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "app-src"
    _git("init", "-q", "-b", "main", str(src))
    (src / "a.txt").write_text("v1\n")
    _git("-C", str(src), "add", "a.txt")
    _git("-C", str(src), "commit", "-q", "-m", "base")
    bare = tmp_path / "app.git"
    _git("clone", "-q", "--bare", str(src), str(bare))
    r = _invoke(tmp_path, monkeypatch, "store", "init", "--mode", "full", "--clone", f"app=file://{bare}", "root")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    root = (tmp_path / "root").resolve()
    r = _invoke(root, monkeypatch, "workspace", "init", ".")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert (root / ".grip" / "workspace_spec.toml").is_file() and not (root / "grip.toml").exists()  # control: adopted
    return root


def test_a_full_root_with_a_workspace_spec_is_still_a_full_mode_root(adopted):
    assert is_full_mode_root(adopted) is True


def test_a_full_mode_tree_with_a_grip_toml_is_not_a_full_mode_root(adopted):
    (adopted / "grip.toml").write_text("")
    assert is_full_mode_root(adopted) is False


def test_a_spec_workspace_with_no_full_mode_tree_is_not_a_full_mode_root(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    ws = tmp_path / "ws"
    _git("init", "-q", "-b", "main", str(ws / "app"))
    (ws / "app" / "a.txt").write_text("v1\n")
    _git("-C", str(ws / "app"), "add", "a.txt")
    _git("-C", str(ws / "app"), "commit", "-q", "-m", "base")
    r = _invoke(ws, monkeypatch, "workspace", "init", ".")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert (ws / ".grip" / "workspace_spec.toml").is_file()  # control: a spec workspace
    assert is_full_mode_root(ws) is False


def test_a_store_verb_at_an_adopted_full_root_names_it_full_mode_and_offers_no_init(adopted, monkeypatch):
    r = _invoke(adopted, monkeypatch, "store", "status")
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert r.stderr.strip() == (
        f"no grip.toml at {adopted} and no committed spec at HEAD; {adopted} is a full-mode gr2 root (no grip.toml; "
        "members under <member>.griplink), and this verb does not read a full-mode root yet; `gr2 status` reads it")
    assert "store init here" not in r.stderr


def test_sets_at_an_absent_revision_of_an_adopted_full_root_refuse(adopted):
    assert set(declared_sets.read(adopted).members) == {"app"}  # control: HEAD reads the root's .gitmodules
    with pytest.raises(DeclaredSetError, match="^root_commit_absent: "):
        declared_sets.read(adopted, "0" * 40)


def _emit(root: Path) -> None:
    events.emit(EventType.REVIEW_BOUND, root, "x", "u", {"n": 1})


def _git_dir_events(root: Path) -> Path:
    return Path(_git("-C", str(root), "rev-parse", "--absolute-git-dir")) / "grip-events"


def test_adopted_full_root_with_old_events_refuses_the_read_by_name(adopted):
    old = adopted / ".grip" / "events"
    old.mkdir(parents=True, exist_ok=True)
    (old / "outbox.jsonl").write_text('{"seq": 1, "type": "review.bound"}\n')
    with pytest.raises(events.EventsLeftBehindError, match="^events_left_behind: ") as exc:
        events.read_events(adopted, "c")
    assert f"Move {old} aside first; it holds events written before full mode." in str(exc.value)
    assert str(old) in str(exc.value) and str(_git_dir_events(adopted)) in str(exc.value)


def test_adopted_full_root_with_old_events_refuses_the_write_and_writes_nothing(adopted):
    old = adopted / ".grip" / "events"
    (old / "cursors").mkdir(parents=True, exist_ok=True)
    (old / "cursors" / "c.json").write_text('{"last_seq": 1}')
    with pytest.raises(events.EventEmitError, match="^events_left_behind: ") as exc:
        _emit(adopted)
    assert str(old) in str(exc.value) and str(_git_dir_events(adopted)) in str(exc.value)
    assert not _git_dir_events(adopted).exists()


@pytest.mark.parametrize("old_dir", ["empty", "absent"])
def test_adopted_full_root_with_no_old_events_reads_the_git_dir(adopted, old_dir):
    old = adopted / ".grip" / "events"
    if old_dir == "empty":
        (old / "cursors").mkdir(parents=True, exist_ok=True)  # dirs alone hold no events
    else:
        assert not old.exists()
    _emit(adopted)
    assert (_git_dir_events(adopted) / "outbox.jsonl").is_file()
    assert [e["type"] for e in events.read_events(adopted, "c")] == ["review.bound"]


def test_native_root_with_events_reads_its_own_events_dir(adopted):
    (adopted / "grip.toml").write_text("")
    _emit(adopted)
    assert (adopted / ".grip" / "events" / "outbox.jsonl").is_file()
    assert [e["type"] for e in events.read_events(adopted, "c")] == ["review.bound"]
    assert not _git_dir_events(adopted).exists()

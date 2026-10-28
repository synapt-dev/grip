"""The workspace-root walk finds a full-mode root from inside its members, and nearest still wins.

THE DEFECT: a full-mode root (`store init --mode full`) has no grip.toml and no workspace_spec.toml, so the walk
every finder verb shares (`workspace_root._resolve_workspace_root`, `context.resolve_root`) found nothing above a
member checkout and fell back to the current directory. From `R/api` a verb took the member's pin checkout as the
workspace root; top-level `status`, which walks with `root_verbs.find`, found `R` from the same place.

THE CONTRACT these rows pin:
  * the walk accepts the full-mode shape root_verbs.find accepts (a git root with a stook, or whose HEAD holds a
    `<m>.griplink/001.2_pin` gitlink), from the root, from `<m>.griplink`, and from `<m>`;
  * NEAREST wins, as before: a store nested inside a full root's member, and a member that is itself a full root
    (its HEAD carries a griplink gitlink), each resolve to themselves, never to the outer root;
  * every command in the app's tree that walks for its root, run from inside a member, lands on the full root.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer

from gr2.python_cli import context, workspace_root
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _bare_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / f"{name}-src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("v1\n")
    _git(src, "add", "a.txt")
    _git(src, "commit", "-q", "-m", "base")
    url = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    return url


def _full_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, members: dict[str, Path]) -> Path:
    monkeypatch.chdir(tmp_path)
    r = make_cli_runner().invoke(app, ["store", "init", "--mode", "full", name])
    assert r.exit_code == 0, r.stderr
    root = tmp_path / name
    monkeypatch.chdir(root)
    for member, url in members.items():
        r = make_cli_runner().invoke(app, ["add", str(url), member])
        assert r.exit_code == 0, (r.stdout, r.stderr)
    assert not (root / "grip.toml").exists()  # control: this is the shape with no manifest file
    return root.resolve()


@pytest.fixture
def full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return _full_root(tmp_path, monkeypatch, "R", {"api": _bare_source(tmp_path, "api")})


@pytest.mark.parametrize("where", [".", "api", "api.griplink", "api.griplink/001.2_pin"])
def test_the_walk_finds_the_full_root_from_inside(full, where, monkeypatch):
    monkeypatch.chdir(full / where)
    assert workspace_root._resolve_workspace_root() == full
    assert Path(context.resolve_root(None).value) == full


def test_a_full_root_with_a_stook_and_no_member_yet_is_found(tmp_path, monkeypatch):
    # Right after `store init --mode full` and before any `add`, the root holds the STOOK and no
    # <member>.griplink gitlink, so only the stook arm can recognise it.
    from gr2.python_cli import root_verbs

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    root = _full_root(tmp_path, monkeypatch, "E", {})
    assert root_verbs._has_stook(root)  # control: the arm under test applies
    assert not root_verbs._has_griplink_gitlink(root)  # control: the other arm does not
    sub = root / "notes"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert workspace_root._resolve_workspace_root() == root


# The full-mode predicate behind the "full-mode root" refusal (init advice, the grip_cli head). Every full root in the
# walk rows above carries BOTH a stook and a gitlink, so each shape it must accept is given alone here.
def test_the_full_mode_predicate_accepts_a_stook_alone(tmp_path, monkeypatch):
    from gr2.python_cli import root_verbs
    from gr2.python_cli.workspace_guidance import is_full_mode_root

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    root = _full_root(tmp_path, monkeypatch, "F", {})  # right after `store init --mode full`, before any `add`
    assert root_verbs._has_stook(root) and not root_verbs._has_griplink_gitlink(root)  # control: stook only
    assert is_full_mode_root(root)


def test_the_full_mode_predicate_accepts_a_gitlink_alone(tmp_path, monkeypatch):
    from gr2.python_cli import griplink, root_verbs
    from gr2.python_cli.workspace_guidance import is_full_mode_root

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    root = _full_root(tmp_path, monkeypatch, "C", {"api": _bare_source(tmp_path, "api")})
    _git(root, "update-ref", "-d", griplink.STOOK)  # a default clone of a full root does not fetch the stook's ref
    assert not root_verbs._has_stook(root) and root_verbs._has_griplink_gitlink(root)  # control: gitlink only
    assert is_full_mode_root(root)


def test_the_full_mode_predicate_refuses_a_plain_subdirectory_of_a_full_root(tmp_path, monkeypatch):
    from gr2.python_cli import root_verbs
    from gr2.python_cli.workspace_guidance import is_full_mode_root

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    root = _full_root(tmp_path, monkeypatch, "S", {})
    sub = root / "notes"
    sub.mkdir()
    assert root_verbs._has_stook(sub)  # control: run in a subdirectory, rev-parse reads the enclosing root's stook
    assert not is_full_mode_root(sub)


def test_a_store_nested_in_a_member_is_nearest(full, monkeypatch):
    # `store init` refuses inside another repo, so the nested store is made by hand with the two markers a store
    # root is read by: its own .git and a grip.toml beside it.
    inner = (full / "api" / "inner").resolve()
    inner.mkdir()
    _git(inner, "init", "-q", "-b", "main")
    (inner / "grip.toml").write_text("")
    (inner / "sub").mkdir()
    monkeypatch.chdir(inner / "sub")
    assert workspace_root._resolve_workspace_root() == inner
    monkeypatch.chdir(full / "api")  # control: one level up, still the outer root
    assert workspace_root._resolve_workspace_root() == full


def test_a_member_that_is_itself_a_full_root_is_nearest(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    inner = _full_root(tmp_path, monkeypatch, "I", {"api": _bare_source(tmp_path, "api")})
    inner_url = tmp_path / "I.git"
    _git(tmp_path, "clone", "-q", "--bare", str(inner), str(inner_url))
    outer = _full_root(tmp_path, monkeypatch, "O", {"inner": inner_url})
    pin = (outer / "inner.griplink" / "001.2_pin").resolve()
    assert "api.griplink" in _git(pin, "ls-tree", "--name-only", "HEAD")  # control: the member carries a gitlink
    monkeypatch.chdir(outer / "inner")
    assert workspace_root._resolve_workspace_root() == pin
    (pin / "sub").mkdir()  # below the pin, so a walk that falls back to cwd cannot land on the pin by accident
    monkeypatch.chdir(pin / "sub")
    assert workspace_root._resolve_workspace_root() == pin
    monkeypatch.chdir(outer / "inner.griplink")  # control: above the pin checkout, the outer root
    assert workspace_root._resolve_workspace_root() == outer


def _leaves(cmd, path: tuple[str, ...] = ()):
    if getattr(cmd, "commands", None) is not None:  # a group (typer vendors its own click)
        for name, sub in sorted(cmd.commands.items()):
            yield from _leaves(sub, (*path, name))
    else:
        yield path, cmd


LEAVES = [(" ".join(p), p) for p, _ in _leaves(typer.main.get_command(app))]


class _Resolved(Exception):
    """Raised by the instrumented predicate once the walk has chosen, so no verb goes on to act."""


def test_the_sweep_walks_the_app_tree():
    # Control: the row below is parametrized over this list; an empty or tiny tree would pass it vacuously.
    assert len(LEAVES) > 60, len(LEAVES)


@pytest.mark.parametrize("words", [p for _, p in LEAVES], ids=[n for n, _ in LEAVES])
def test_every_verb_that_walks_lands_on_the_full_root(full, words, monkeypatch):
    """Run from inside a member with no arguments. If the verb walks for its root, the walk must say yes at the full
    root; a walk that says no all the way up is a verb about to take the member as its root."""
    real = workspace_root._is_workspace_root
    asked: list[tuple[Path, bool]] = []

    def predicate(path: Path) -> bool:
        yes = real(path)
        asked.append((Path(path).resolve(), yes))
        if yes:
            raise _Resolved(path)
        return yes

    monkeypatch.setattr(workspace_root, "_is_workspace_root", predicate)
    monkeypatch.setenv("PATH", str(Path(subprocess.run(["which", "git"], capture_output=True, text=True,
                                                       check=True).stdout.strip()).parent))
    monkeypatch.chdir(full / "api")
    make_cli_runner().invoke(app, list(words))
    if not asked:
        return  # this verb does not walk (or refused on usage before walking)
    chosen = [p for p, yes in asked if yes]
    assert chosen and chosen[0] == full, (words, asked)


def _root_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.relative_to(root).parts[:1])


@pytest.mark.parametrize("words", [p for _, p in LEAVES], ids=[n for n, _ in LEAVES])
def test_every_verb_run_inside_a_member_writes_nothing_and_offers_no_init(full, words, monkeypatch):
    """The same walk, run for real: a verb that refuses on a full root writes nothing into it (a stray .grip/ made
    every later verb read the root as a broken store), and never advises an init, since the root already is one."""
    monkeypatch.setenv("PATH", str(Path(subprocess.run(["which", "git"], capture_output=True, text=True,
                                                       check=True).stdout.strip()).parent))
    before = _root_files(full)
    monkeypatch.chdir(full / "api")
    r = make_cli_runner().invoke(app, list(words))
    out = (r.stdout or "") + (r.stderr or "")
    assert _root_files(full) == before, (words, out)
    assert "gr2 store init" not in out and "gr2 workspace init" not in out, (words, out)


def test_a_grip_toml_without_git_still_encloses(tmp_path):
    """Widening the walk did not narrow it: a parent holding a grip.toml FILE and no .git still encloses a directory
    below it, as it did before full-mode roots were read. Control: the same parent
    without the file encloses nothing."""
    from gr2.python_cli import workspace_guidance

    parent = tmp_path / "ws"
    child = parent / "sub"
    child.mkdir(parents=True)
    assert workspace_guidance.enclosing_workspace_root(child) is None
    (parent / "grip.toml").write_text('schema_version = 1\nworkspace_name = "ws"\n')
    assert not (parent / ".git").exists()
    assert workspace_guidance.enclosing_workspace_root(child) == parent.resolve()

"""Hidden `gr2 store` aliases decide inside a member exactly as their top-level verbs do.

Inside a member checkout, with no -C, `store add` and `store commit` refuse at 2 naming git unless the store's
grip.toml says inside_member = "root"; a bad inside_member value refuses every alias at 2. An explicit -C names
the root and runs, which is how the top-level verbs call these aliases.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def gr2(cwd: Path, monkeypatch, *args: str):
    monkeypatch.chdir(cwd)
    return make_cli_runner().invoke(app, list(args))


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> dict:
    """A compat store S with one member `api`, its pins recorded in one root commit."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "f").write_text("x\n")
    git(src, "add", "f")
    git(src, "commit", "-q", "-m", "m")
    url = tmp_path / "api.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    root = tmp_path / "S"
    r = gr2(tmp_path, monkeypatch, "store", "init", "--clone", f"api={url}", str(root))
    assert r.exit_code == 0, r.stderr
    r = gr2(root, monkeypatch, "store", "commit", "-m", "pins")
    assert r.exit_code == 0, r.stderr
    return {"root": root, "url": url}


def _set_inside_member(root: Path, value: str) -> None:
    """Add the key at the top level beside `mode` (prepended: the file ends in [[...]] tables, and a key appended
    there would belong to the last table) and record it, so the root stays clean for store commit."""
    doc = root / "grip.toml"
    doc.write_text(f"inside_member = {value}\n" + doc.read_text())
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "-m", "inside_member")
    assert tomllib.loads(git(root, "show", "HEAD:grip.toml"))["inside_member"] == tomllib.loads(f"v = {value}")["v"]


def _move_member(root: Path) -> None:
    """A new member commit, pushed: a compat store records only a pin its origin has."""
    (root / "api" / "g").write_text("y\n")
    git(root / "api", "add", "g")
    git(root / "api", "commit", "-q", "-m", "moved")
    git(root / "api", "push", "-q", "origin", "HEAD:main")


def test_store_commit_inside_a_member_refuses_and_writes_nothing(store, monkeypatch):
    root = store["root"]
    _move_member(root)
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "commit", "-m", "inside")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert "use git commit here" in r.stderr
    assert git(root, "rev-parse", "HEAD") == head


def test_store_add_inside_a_member_refuses_and_clones_nothing(store, monkeypatch):
    root = store["root"]
    r = gr2(root / "api", monkeypatch, "store", "add", f"web={store['url']}")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert "use git add here" in r.stderr
    assert not (root / "web").exists() and not (root / "api" / "web").exists()


def test_inside_member_root_lets_store_commit_and_add_act_on_the_root(store, monkeypatch):
    root = store["root"]
    _set_inside_member(root, '"root"')
    _move_member(root)
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "commit", "-m", "from inside")
    assert r.exit_code == 0, r.stderr
    assert git(root, "rev-parse", "HEAD") != head
    assert git(root, "rev-parse", "HEAD:api") == git(root / "api", "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "add", f"web={store['url']}")
    assert r.exit_code == 0, r.stderr
    assert (root / "web" / "f").read_text() == "x\n"
    assert not (root / "api" / "web").exists()


def test_inside_member_root_acts_on_the_store_root_past_a_member_spec_workspace(store, monkeypatch):
    """A member that carries its own .grip/workspace_spec.toml is a workspace to the older nearest-workspace walk,
    which would stop there; the store root that decided is still the root acted on."""
    root = store["root"]
    _set_inside_member(root, '"root"')
    (root / "api" / ".grip").mkdir()
    (root / "api" / ".grip" / "workspace_spec.toml").write_text("")
    git(root / "api", "add", ".grip/workspace_spec.toml")
    _move_member(root)
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "commit", "-m", "past the spec")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert git(root, "rev-parse", "HEAD") != head
    assert git(root, "rev-parse", "HEAD:api") == git(root / "api", "rev-parse", "HEAD")


@pytest.mark.parametrize("value", ['"rot"', "true", '""'])
def test_a_bad_inside_member_value_refuses_every_store_alias(store, monkeypatch, value):
    root = store["root"]
    _set_inside_member(root, value)
    _move_member(root)
    head = git(root, "rev-parse", "HEAD")
    for cwd in (root, root / "api"):
        for args in (("store", "commit", "-m", "x"), ("store", "add", f"web={store['url']}"),
                     ("store", "checkout", "HEAD")):
            r = gr2(cwd, monkeypatch, *args)
            assert r.exit_code == 2, (cwd, args, r.stderr)
            assert "inside_member" in r.stderr and '"refuse"' in r.stderr and '"root"' in r.stderr
    assert git(root, "rev-parse", "HEAD") == head
    assert not (root / "web").exists()


def test_store_checkout_inside_a_member_acts_on_the_root(store, monkeypatch):
    """checkout is not one of git's in-member verbs, so like top-level checkout it runs on the root above: the member
    goes back to the pin the ROOT records, which only the root acting can do."""
    root = store["root"]
    recorded = git(root, "rev-parse", "HEAD:api")
    head = git(root, "rev-parse", "HEAD")
    _move_member(root)
    assert git(root / "api", "rev-parse", "HEAD") != recorded
    r = gr2(root / "api", monkeypatch, "store", "checkout", "HEAD")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert git(root / "api", "rev-parse", "HEAD") == recorded
    assert git(root, "rev-parse", "HEAD") == head


def test_an_explicit_root_names_the_root_and_runs_from_inside_a_member(store, monkeypatch):
    root = store["root"]
    _move_member(root)
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "commit", "-C", str(root), "-m", "named")
    assert r.exit_code == 0, r.stderr
    assert git(root, "rev-parse", "HEAD") != head


def test_an_explicit_root_naming_the_member_itself_refuses_and_writes_nothing(store, monkeypatch):
    root = store["root"]
    _move_member(root)
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "commit", "-C", str(root / "api"), "-m", "member as root")
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert "no grip.toml at" in r.stderr
    assert git(root, "rev-parse", "HEAD") == head


def test_store_commit_from_a_plain_subdirectory_of_the_root_commits_the_root(store, monkeypatch):
    """A directory under the root that is not a member checkout is the root's own tree: no refusal there."""
    root = store["root"]
    _move_member(root)
    (root / "notes").mkdir()
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "notes", monkeypatch, "store", "commit", "-m", "from notes")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert git(root, "rev-parse", "HEAD") != head
    assert git(root, "rev-parse", "HEAD:api") == git(root / "api", "rev-parse", "HEAD")


def test_store_add_from_a_nested_member_subdirectory_refuses_and_clones_nothing(store, monkeypatch):
    root = store["root"]
    (root / "api" / "deep" / "er").mkdir(parents=True)
    r = gr2(root / "api" / "deep" / "er", monkeypatch, "store", "add", f"web={store['url']}")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert "use git add here" in r.stderr
    assert not (root / "web").exists() and not (root / "api" / "web").exists()
    assert not (root / "api" / "deep" / "er" / "web").exists()


def test_a_bad_inside_member_value_refuses_store_checkout_and_the_member_does_not_move(store, monkeypatch):
    root = store["root"]
    _set_inside_member(root, '"rot"')
    _move_member(root)
    moved = git(root / "api", "rev-parse", "HEAD")
    assert moved != git(root, "rev-parse", "HEAD:api")
    r = gr2(root / "api", monkeypatch, "store", "checkout", "HEAD")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert git(root / "api", "rev-parse", "HEAD") == moved


def _griplink_root(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A griplink root R (`store init --mode full`) with one member `api`; returns (root, the member's bare url)."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "src-api"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("v1\n")
    git(src, "add", "a.txt")
    git(src, "commit", "-q", "-m", "base")
    url = tmp_path / "api.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    r = gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", "R")
    assert r.exit_code == 0, r.stderr
    root = tmp_path / "R"
    r = gr2(root, monkeypatch, "add", str(url), "api")
    assert r.exit_code == 0, r.stderr
    return root, url


def test_store_commit_and_add_inside_a_griplink_member_refuse_and_write_nothing(tmp_path: Path, monkeypatch):
    """A griplink root decides through the same resolve: inside its member, store commit and store add refuse at 2
    naming git, as top-level gr2 commit and gr2 add do there, and the root's refs and tree are unchanged."""
    root, url = _griplink_root(tmp_path, monkeypatch)
    (root / "api" / "a.txt").write_text("v2\n")
    git(root / "api", "add", "a.txt")
    refs = git(root, "for-each-ref", "--format=%(refname) %(objectname)")
    r = gr2(root / "api", monkeypatch, "store", "commit", "-m", "inside")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert "use git commit here" in r.stderr
    r = gr2(root / "api", monkeypatch, "store", "add", f"web={url}")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert "use git add here" in r.stderr
    assert git(root, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert not (root / "web").exists() and not (root / "api" / "web").exists()


def test_store_checkout_inside_a_griplink_member_still_refuses_at_5_and_moves_nothing(tmp_path: Path, monkeypatch):
    """checkout does not refuse inside a member. The walk finds the griplink root above as a full-mode root, and
    store checkout refuses there at 5 naming it as one, with no init advice; top-level gr2 checkout acts there.
    Pinned as it is: making store checkout act like gr2 checkout is a separate change."""
    root, _ = _griplink_root(tmp_path, monkeypatch)
    (root / "api" / "a.txt").write_text("v2\n")
    git(root / "api", "commit", "-q", "-am", "moved")
    moved = git(root / "api", "rev-parse", "HEAD")
    refs = git(root, "for-each-ref", "--format=%(refname) %(objectname)")
    r = gr2(root / "api", monkeypatch, "store", "checkout", "HEAD")
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert "is a full-mode gr2 root" in r.stderr
    assert "store init" not in r.stderr, "a full-mode root is already a workspace"
    assert git(root / "api", "rev-parse", "HEAD") == moved
    assert git(root, "for-each-ref", "--format=%(refname) %(objectname)") == refs


def test_store_push_inside_a_member_writes_nothing(store, monkeypatch):
    """store push reads the cwd only (no upward walk), so inside a member it never reaches the root; pinned so it
    cannot drift from top-level push, which refuses there naming git push."""
    root = store["root"]
    remote_before = git(store["url"], "rev-parse", "refs/heads/main")
    head = git(root, "rev-parse", "HEAD")
    r = gr2(root / "api", monkeypatch, "store", "push")
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert "no grip.toml at" in r.stderr
    assert git(store["url"], "rev-parse", "refs/heads/main") == remote_before
    assert git(root, "rev-parse", "HEAD") == head

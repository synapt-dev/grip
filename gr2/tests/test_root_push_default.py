"""gr2 push at a griplink root pushes the root, with no flag; --root is accepted and changes nothing.

Each row checks the exact exit code, the text, that no traceback escaped, and origin's refs before and after.
The fixture's git reads no global config and an empty hooks dir, and origin is a local bare repo.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

from gr2.python_cli import griplink
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

FETCH = "git fetch origin '+refs/dev.synapt.grip/*:refs/dev.synapt.grip/*'"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


def refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(objectname) %(refname)")


def gr2(cwd: Path, monkeypatch, *args: str):
    monkeypatch.chdir(cwd)
    return make_cli_runner().invoke(app, list(args))


def no_trace(r) -> None:
    assert "Traceback" not in (r.stdout or "") + (r.stderr or "")
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict:
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always",
           "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": str(hooks)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "src-api"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("v1\n")
    git(src, "add", "a.txt")
    git(src, "commit", "-q", "-m", "base")
    url = tmp_path / "api.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    assert gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", "R").exit_code == 0
    root = tmp_path / "R"
    r = gr2(root, monkeypatch, "add", str(url), "api")
    assert r.exit_code == 0, r.stderr
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "remote", "add", "origin", str(origin))
    return {"tmp": tmp_path, "root": root, "origin": origin, "hooks": hooks}


def _member_commit(root: Path, text: str) -> str:
    member = root / "api"
    (member / "a.txt").write_text(text)
    git(member, "commit", "-q", "-am", text.strip())
    return git(member, "rev-parse", "HEAD")


def test_bare_push_at_a_root_pushes_the_root(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    assert refs(origin) == ""
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 0, r.stderr
    assert git(origin, "rev-parse", "refs/heads/main") == git(root, "rev-parse", "HEAD")
    assert git(origin, "rev-parse", griplink.STOOK) == git(root, "rev-parse", griplink.STOOK)
    r = gr2(root, monkeypatch, "push", "--root")
    no_trace(r)
    assert r.exit_code == 0, r.stderr


def test_a_plain_clone_with_no_names_fetched_refuses_and_names_the_fetch(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    assert gr2(root, monkeypatch, "push").exit_code == 0
    clone = world["tmp"] / "plain"
    git(world["tmp"], "clone", "-q", str(origin), str(clone))
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "-q", "--verify", griplink.STOOK],
                          capture_output=True).returncode != 0
    before = refs(origin)
    r = gr2(clone, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 4, (r.stdout, r.stderr)
    assert r.stderr.strip() == (f"gr2 push: this workspace's names were not fetched (a plain git clone has main only);"
                                f" run {FETCH}, then gr2 push again")
    assert refs(origin) == before


def test_git_refusing_the_push_prints_git_words_and_moves_nothing(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    hooks = world["tmp"] / "deny-hooks"
    hooks.mkdir()
    hook = hooks / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'denied by the origin test hook' >&2\nexit 1\n")
    hook.chmod(0o755)
    git(origin, "config", "core.hooksPath", str(hooks))  # receive-pack reads origin's own config
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert "denied by the origin test hook" in r.stderr
    assert refs(origin) == ""


def test_a_store_root_still_refuses_with_the_store_hint(tmp_path, world, monkeypatch):
    src = tmp_path / "src-s"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "f").write_text("x\n")
    git(src, "add", "f")
    git(src, "commit", "-q", "-m", "m")
    url = tmp_path / "s.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(url))
    store = tmp_path / "S"
    assert gr2(tmp_path, monkeypatch, "store", "init", "--clone", f"api={url}", str(store)).exit_code == 0
    git(store, "remote", "add", "origin", str(world["origin"]))
    for args in (("push",), ("push", "--root")):
        r = gr2(store, monkeypatch, *args)
        no_trace(r)
        assert r.exit_code == 4, (args, r.stdout, r.stderr)
        assert r.stderr.strip() == "gr2 push at a workspace root is not available yet; gr2 store push pushes this store"
    assert refs(world["origin"]) == ""


def test_a_root_on_another_branch_refuses_by_name(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    git(root, "checkout", "-q", "-b", "side")
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 3, (r.stdout, r.stderr)
    assert r.stderr.strip() == (
        "side: full mode works on the root branch main only for now; "
        "next: git switch main (or git branch -m side main), "
        "then gr2 push again")
    assert refs(origin) == ""


def test_push_help_lists_allow_rewind(world, monkeypatch):
    monkeypatch.setenv("COLUMNS", "2000")
    r = gr2(world["root"], monkeypatch, "push", "--help")
    assert r.exit_code == 0
    assert "--allow-rewind" in strip_ansi(r.stdout)
    assert "--root" not in strip_ansi(r.stdout)


def _second_member(world, monkeypatch) -> None:
    tmp, root = world["tmp"], world["root"]
    src = tmp / "src-web"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "w.txt").write_text("w1\n")
    git(src, "add", "w.txt")
    git(src, "commit", "-q", "-m", "base")
    url = tmp / "web.git"
    git(tmp, "clone", "-q", "--bare", str(src), str(url))
    r = gr2(root, monkeypatch, "add", str(url), "web")
    assert r.exit_code == 0, r.stderr
    _member_commit(root, "v2\n")
    (root / "web" / "w.txt").write_text("w2\n")
    git(root / "web", "commit", "-q", "-am", "w2")
    griplink.commit(root, "both", stage_all=True)


def test_one_push_moves_the_root_and_every_member_together(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    _second_member(world, monkeypatch)
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 0, r.stderr
    for ref in (griplink.MAIN, griplink.SHADOW, griplink.STOOK, griplink.member_ref("api"), griplink.member_ref("web")):
        assert git(origin, "rev-parse", ref) == git(root, "rev-parse", ref), ref


def test_origin_refusing_one_member_ref_lands_nothing(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    _second_member(world, monkeypatch)
    hooks = world["tmp"] / "update-hooks"
    hooks.mkdir()
    hook = hooks / "update"
    hook.write_text('#!/bin/sh\ncase "$1" in */__members__/web/*) echo "web refused by the test hook" >&2; exit 1;; esac\n')
    hook.chmod(0o755)
    git(origin, "config", "core.hooksPath", str(hooks))
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 5, (r.stdout, r.stderr)
    assert "web refused by the test hook" in r.stderr
    assert refs(origin) == ""


def _push_a_then_b(world, monkeypatch) -> tuple[dict, dict]:
    root = world["root"]
    assert gr2(root, monkeypatch, "push").exit_code == 0
    a = {ref: git(root, "rev-parse", ref) for ref in (griplink.MAIN, griplink.SHADOW, griplink.STOOK)}
    _member_commit(root, "v2\n")
    griplink.commit(root, "b", stage_all=True)
    assert gr2(root, monkeypatch, "push").exit_code == 0
    b = {ref: git(root, "rev-parse", ref) for ref in a}
    assert a != b
    return a, b


def test_origin_ahead_of_the_root_refuses_fetch_first(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    a, b = _push_a_then_b(world, monkeypatch)
    for ref, sha in a.items():
        git(root, "update-ref", ref, sha)
    before = refs(origin)
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 3, (r.stdout, r.stderr)
    assert r.stderr.strip() == (f"fetch first: the remote shadow moved (remote main is {b[griplink.MAIN][:12]},"
                                f" not an ancestor of {a[griplink.MAIN][:12]}); nothing was pushed")
    assert refs(origin) == before


def test_a_member_rewind_refuses_and_allow_rewind_lands_it(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    _member_commit(root, "v2\n")
    griplink.commit(root, "a", stage_all=True)
    assert gr2(root, monkeypatch, "push").exit_code == 0
    pushed = git(origin, "rev-parse", griplink.member_ref("api"))
    new = git(root, "commit-tree", f"{pushed}^{{tree}}", "-m", "rewritten")
    git(root, "update-ref", griplink.member_ref("api"), new)
    (root / "README.md").write_text("root change\n")
    griplink.commit(root, "rewind", stage_all=True)
    before = refs(origin)
    r = gr2(root, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 3, (r.stdout, r.stderr)
    assert r.stderr.strip() == (f"api: {new[:12]} does not descend from {pushed[:12]}, which the remote"
                                " shadow recorded (a rewind; --allow-rewind records one); nothing was pushed")
    assert refs(origin) == before
    r = gr2(root, monkeypatch, "push", "--allow-rewind")
    no_trace(r)
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert git(origin, "rev-parse", griplink.member_ref("api")) == git(root, "rev-parse", griplink.member_ref("api"))
    assert git(origin, "rev-parse", griplink.MAIN) == git(root, "rev-parse", griplink.MAIN)
    tip = git(root, "rev-parse", griplink.MAIN)
    assert f"api: rewind: {pushed} -> {new}" in git(origin, "log", "-1", "--format=%B", griplink.rewound_ref(tip))

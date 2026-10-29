"""The griplink verbs through the CLI: the engine is chosen by the shape of the root above the current directory.

A griplink root (full mode) runs griplink; an older-shape store root runs the store engine; outside any root the
one-repo forms are unchanged. The proof row walks init, add, commit, status, a plain clone's checkout and promote
through gr2 alone, plus git add inside the member.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import declared_sets, griplink
from gr2.python_cli.app import app
from gr2.python_cli.grip_cli import STORE_COMMIT_UNCHANGED

from tests.conftest import make_cli_runner

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def gr2(cwd: Path, monkeypatch, *args: str):
    monkeypatch.chdir(cwd)
    return make_cli_runner().invoke(app, list(args))


def _ref(repo: Path, ref: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref], capture_output=True, text=True)
    return done.stdout.strip() or None


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict:
    """A member remote `api` (one file) and a griplink root R made by `gr2 store init --mode full` and `gr2 add`."""
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
    return {"tmp": tmp_path, "root": root, "url": url}


def _edit_and_stage(root: Path) -> None:
    (root / "api" / "a.txt").write_text("v2\n")
    git(root / "api", "add", "a.txt")


def test_proof_init_add_commit_status_clone_checkout_promote(world, monkeypatch):
    root = world["root"]
    assert git(root, "ls-tree", "HEAD", "api.griplink/001.2_pin").startswith("160000 commit ")
    pin = git(root / "api", "rev-parse", "HEAD")
    _edit_and_stage(root)
    r = gr2(root, monkeypatch, "commit", "-m", "change a")
    assert r.exit_code == 0, r.stderr
    assert _ref(root, griplink.member_ref("api"))
    assert git(root / "api", "rev-parse", "HEAD") == pin  # the member's git is untouched until promote
    r = gr2(root, monkeypatch, "status")
    assert r.exit_code == 0 and r.stdout.strip() == "status: on main, upstream none; nothing to commit", r.stdout
    clone = world["tmp"] / "C"
    git(world["tmp"], "clone", "-q", str(root), str(clone))
    r = gr2(clone, monkeypatch, "checkout")
    assert r.exit_code == 0, r.stderr
    assert (clone / "api" / "a.txt").read_text() == "v2\n"
    assert git(clone / "api", "status", "--short") == "M  a.txt"
    r = gr2(root, monkeypatch, "promote", "api")
    assert r.exit_code == 0, r.stderr
    assert git(root / "api", "log", "-1", "--format=%s") == "change a"
    assert git(root / "api", "rev-parse", "HEAD^") == pin
    assert _ref(root, griplink.member_ref("api")) is None


def test_commit_inside_a_member_refuses_and_writes_nothing(world, monkeypatch):
    root = world["root"]
    _edit_and_stage(root)
    stook = _ref(root, griplink.STOOK)
    r = gr2(root / "api", monkeypatch, "commit", "-m", "inside")
    assert r.exit_code == 2
    assert "use git commit here" in r.stderr
    assert _ref(root, griplink.STOOK) == stook and _ref(root, griplink.member_ref("api")) is None


def test_inside_member_root_commits_the_root_from_a_member(world, monkeypatch):
    root = world["root"]
    (root / "grip.toml").write_text('inside_member = "root"\n')
    _edit_and_stage(root)
    r = gr2(root / "api", monkeypatch, "commit", "-m", "from inside")
    assert r.exit_code == 0, r.stderr
    assert _ref(root, griplink.member_ref("api"))


def test_an_unknown_inside_member_value_refuses_every_root_verb(world, monkeypatch):
    root = world["root"]
    (root / "grip.toml").write_text('inside_member = "rot"\n')
    for args in (("status",), ("commit", "-m", "x")):
        r = gr2(root, monkeypatch, *args)
        assert r.exit_code == 2, args
        assert "inside_member" in r.stderr and '"refuse"' in r.stderr and '"root"' in r.stderr


@pytest.mark.parametrize("value", ["true", '""'])
def test_an_inside_member_value_of_another_type_refuses_and_writes_nothing(world, monkeypatch, value):
    root = world["root"]
    (root / "grip.toml").write_text(f"inside_member = {value}\n")
    _edit_and_stage(root)
    stook = _ref(root, griplink.STOOK)
    for cwd in (root, root / "api"):
        r = gr2(cwd, monkeypatch, "commit", "-m", "x")
        assert r.exit_code == 2, (cwd, r.stderr)
        assert "inside_member" in r.stderr and '"refuse"' in r.stderr and '"root"' in r.stderr
    assert _ref(root, griplink.STOOK) == stook and _ref(root, griplink.member_ref("api")) is None


def test_a_plain_clone_checks_out_with_its_names_fetched_then_commits(world, monkeypatch):
    root = world["root"]
    _edit_and_stage(root)
    assert gr2(root, monkeypatch, "commit", "-m", "c").exit_code == 0
    clone = world["tmp"] / "C"
    git(world["tmp"], "clone", "-q", str(root), str(clone))
    assert _ref(clone, griplink.STOOK) is None
    assert gr2(clone, monkeypatch, "checkout").exit_code == 0
    assert _ref(clone, griplink.STOOK) == _ref(root, griplink.STOOK)
    r = gr2(clone, monkeypatch, "commit", "-m", "x")
    assert r.exit_code == 1  # griplink ran: nothing new, in git's words
    assert "nothing to commit" in r.stderr


def test_push_at_a_root_pushes_and_the_hidden_root_flag_changes_nothing(world, monkeypatch):
    root = world["root"]
    remote = world["tmp"] / "root.git"
    git(world["tmp"], "init", "-q", "--bare", "-b", "main", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    r = gr2(root, monkeypatch, "push")
    assert r.exit_code == 0, r.stderr
    assert _ref(remote, "refs/heads/main") == _ref(root, "HEAD")
    r = gr2(root, monkeypatch, "push", "--root")
    assert r.exit_code == 0, r.stderr
    assert _ref(remote, "refs/heads/main") == _ref(root, "HEAD")


def test_a_store_root_runs_the_store_engine_and_refuses_full_mode_only_verbs(tmp_path, monkeypatch):
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
    store = tmp_path / "S"
    r = gr2(tmp_path, monkeypatch, "store", "init", "--clone", f"api={url}", str(store))
    assert r.exit_code == 0, r.stderr
    assert _ref(store, griplink.STOOK) is None
    r = gr2(store, monkeypatch, "commit", "-m", "pins")
    assert r.exit_code == 0, r.stderr
    head = git(store, "rev-parse", "HEAD")  # the store engine recorded the pins: a root commit exists
    assert git(store, "ls-tree", "HEAD", "api").startswith("160000 commit ")
    r = gr2(store, monkeypatch, "commit", "-m", "nothing new")
    assert r.exit_code == 0 and r.stdout.strip() == STORE_COMMIT_UNCHANGED, (r.stdout, r.stderr)
    assert git(store, "rev-parse", "HEAD") == head
    r = gr2(store / "api", monkeypatch, "commit", "-m", "inside")
    assert r.exit_code == 2 and "use git commit here" in r.stderr
    r = gr2(store, monkeypatch, "promote", "api")
    assert r.exit_code == 4
    assert "migrate is not built yet" in r.stderr
    assert "#" not in r.stderr and "m_" not in r.stderr  # no card number or post id in public text
    r = gr2(store, monkeypatch, "push")
    assert r.exit_code == 4 and "gr2 store push" in r.stderr
    r = gr2(store, monkeypatch, "add", str(url), "web")  # the store engine clones and records a second member
    assert r.exit_code == 0, r.stderr
    assert (store / "web" / "f").read_text() == "x\n"
    r = gr2(store, monkeypatch, "checkout")  # the store engine's materialize
    assert r.exit_code == 0, r.stderr


def test_outside_any_root_commit_is_the_one_repo_commit(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "plain"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x\n")
    git(repo, "add", "f")
    r = gr2(repo, monkeypatch, "commit", "-m", "one repo")
    assert r.exit_code == 0, r.stderr
    assert git(repo, "log", "-1", "--format=%s") == "one repo"


def test_full_mode_init_refuses_an_existing_repo_and_member_paths(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    existing = tmp_path / "E"
    existing.mkdir()
    git(existing, "init", "-q")
    r = gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", "E")
    assert r.exit_code == 4 and "already holds a git repository" in r.stderr
    r = gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", "--member", "x", "N")
    assert r.exit_code == 4 and not (tmp_path / "N").exists()


def test_full_mode_init_refuses_a_directory_of_checkouts_and_writes_nothing(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "W"
    (root / "alpha").mkdir(parents=True)
    git(root / "alpha", "init", "-q")
    r = gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", str(root))
    assert r.exit_code == 4 and "already holds git checkouts (alpha)" in r.stderr
    assert not (root / ".git").exists()


def test_init_on_a_griplink_root_never_switches_it_and_a_second_full_init_changes_nothing(world, monkeypatch):
    root = world["root"]
    before = {ref: _ref(root, ref) for ref in ("HEAD", griplink.STOOK)}
    r = gr2(root, monkeypatch, "store", "init", "--mode", "compat", str(root))
    assert r.exit_code == 4 and "init does not switch a workspace's mode" in r.stderr
    assert not (root / "grip.toml").exists()
    r = gr2(root, monkeypatch, "store", "init", "--mode", "full", str(root))
    assert r.exit_code == 0 and "already a full-mode workspace root" in r.stdout
    assert {ref: _ref(root, ref) for ref in before} == before


def _root_state(root: Path) -> tuple:
    return (git(root, "rev-parse", "HEAD"), _ref(root, griplink.STOOK), (root / ".gitmodules").read_text(),
            sorted(p.name for p in root.iterdir()))


def test_add_whose_clone_fails_refuses_with_gits_line_and_leaves_no_member_dir(world, monkeypatch):
    root = world["root"]
    notes = world["tmp"] / "notes.txt"  # outside the root, so the clone runs and fails
    notes.write_text("not a repository\n")
    before = _root_state(root)
    r = gr2(root, monkeypatch, "add", str(notes))
    assert r.exit_code == 3, (r.exit_code, r.stderr)
    assert r.stderr.startswith("gr2 add: git failed: ") and "nothing past that step was written" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr + r.stdout
    assert not (root / "notes.txt.griplink").exists()
    assert _root_state(root) == before


def test_add_of_an_existing_member_refuses_naming_it_and_changes_nothing(world, monkeypatch):
    root = world["root"]
    before = _root_state(root)
    r = gr2(root, monkeypatch, "add", str(world["url"]), "api")
    assert r.exit_code == 3, (r.exit_code, r.stderr)
    assert r.stderr.startswith("api: api.griplink is already at this root"), r.stderr
    assert _root_state(root) == before


def _web(world) -> Path:
    src = world["tmp"] / "src-web"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "w.txt").write_text("w1\n")
    git(src, "add", "w.txt")
    git(src, "commit", "-q", "-m", "base")
    url = world["tmp"] / "web.git"
    git(world["tmp"], "clone", "-q", "--bare", str(src), str(url))
    return url


def _section(root: Path, m: str) -> list[str]:
    lines, on = [], False
    for line in (root / ".gitmodules").read_text().splitlines():
        if line.startswith("["):
            on = line == f'[submodule "{m}"]'
        elif on:
            lines.append(line.strip())
    return lines


def _commit_set_lines_for_fixture(root: Path, m: str, name: str) -> None:
    # no verb writes gr2-set yet, so the fixture uses the helper the verb will call
    griplink._set_lines(root, m, name)
    git(root, "add", ".gitmodules")
    griplink._cut(root, f"set {m}", [git(root, "rev-parse", "HEAD")])


def _two_roots(world, monkeypatch) -> Path:
    """R pushed to a bare remote, and a second root C checked out from it; returns C."""
    root, tmp = world["root"], world["tmp"]
    remote = tmp / "root.git"
    git(tmp, "init", "-q", "--bare", "-b", "main", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    assert gr2(root, monkeypatch, "push").exit_code == 0
    c = tmp / "C"
    git(tmp, "clone", "-q", str(remote), str(c))
    assert gr2(c, monkeypatch, "checkout").exit_code == 0
    return c


def test_add_of_a_second_member_keeps_the_first_members_gr2_set_lines(world, monkeypatch):
    root = world["root"]
    _commit_set_lines_for_fixture(root, "api", "billing")
    api = _section(root, "api")
    assert api[-1:] == ["gr2-set = billing"], api
    r = gr2(root, monkeypatch, "add", str(_web(world)), "web")
    assert r.exit_code == 0, r.stderr
    assert _section(root, "api") == api
    assert sorted(_section(root, "web")) == ["ignore = dirty", "path = web.griplink/" + griplink.PIN,
                                            f"url = {world['tmp'] / 'web.git'}"]
    assert git(root, "show", "HEAD:.gitmodules") == (root / ".gitmodules").read_text().strip()


def test_pull_of_a_lane_on_another_member_keeps_both_members_set_lines(world, monkeypatch):
    root = world["root"]
    assert gr2(root, monkeypatch, "add", str(_web(world)), "web").exit_code == 0
    c = _two_roots(world, monkeypatch)
    _commit_set_lines_for_fixture(root, "api", "billing")
    _commit_set_lines_for_fixture(c, "web", "auth")
    assert gr2(c, monkeypatch, "push").exit_code == 0
    r = gr2(root, monkeypatch, "pull")
    assert r.exit_code == 0, r.stderr
    for text in ((root / ".gitmodules").read_text(), git(root, "show", "HEAD:.gitmodules")):
        assert "gr2-set = billing" in text and "gr2-set = auth" in text, text
    assert _section(root, "api")[-1] == "gr2-set = billing" and _section(root, "web")[-1] == "gr2-set = auth"


def test_pull_of_a_lane_on_the_same_member_refuses_and_leaves_the_root_as_it_was(world, monkeypatch):
    root = world["root"]
    c = _two_roots(world, monkeypatch)
    _commit_set_lines_for_fixture(root, "api", "billing")
    _commit_set_lines_for_fixture(c, "api", "ops")
    assert gr2(c, monkeypatch, "push").exit_code == 0
    head, gm = git(root, "rev-parse", "HEAD"), (root / ".gitmodules").read_bytes()
    r = gr2(root, monkeypatch, "pull")
    assert git(root, "rev-parse", "HEAD") == head
    assert (root / ".gitmodules").read_bytes() == gm
    assert _ref(root, "MERGE_HEAD") is None
    assert r.exit_code == 3, (r.exit_code, r.stderr)
    assert "the root merge stopped: " in r.stderr and "nothing was pulled" in r.stderr, r.stderr


def test_fast_forward_pull_keeps_mine_and_brings_theirs_set_lines(world, monkeypatch):
    root = world["root"]
    assert gr2(root, monkeypatch, "add", str(_web(world)), "web").exit_code == 0
    _commit_set_lines_for_fixture(root, "api", "billing")
    c = _two_roots(world, monkeypatch)
    _commit_set_lines_for_fixture(c, "web", "auth")
    assert gr2(c, monkeypatch, "push").exit_code == 0
    r = gr2(root, monkeypatch, "pull")
    assert r.exit_code == 0, r.stderr
    assert git(root, "rev-parse", "HEAD") == git(c, "rev-parse", "HEAD")
    assert _section(root, "api")[-1] == "gr2-set = billing" and _section(root, "web")[-1] == "gr2-set = auth"


def test_set_lines_writes_one_name_replacing_the_old_line_and_keeps_other_keys(world, monkeypatch):
    root = world["root"]
    assert gr2(root, monkeypatch, "add", str(_web(world)), "web").exit_code == 0
    gm = str(root / ".gitmodules")
    git(root, "config", "-f", gm, "--add", "submodule.web.gr2-set", "auth")
    git(root, "config", "-f", gm, "--add", "submodule.api.gr2-set", "old")
    api, web = _section(root, "api"), _section(root, "web")
    griplink._set_lines(root, "api", "a")
    assert _section(root, "api") == api[:-1] + ["gr2-set = a"]
    assert _section(root, "web") == web
    griplink._set_lines(root, "web", "a.b_c-1")
    assert _section(root, "web") == web[:-1] + ["gr2-set = a.b_c-1"]


def test_set_lines_none_clears_the_members_line_and_keeps_other_keys(world, monkeypatch):
    root = world["root"]
    git(root, "config", "-f", str(root / ".gitmodules"), "--add", "submodule.api.gr2-set", "old")
    api = _section(root, "api")
    griplink._set_lines(root, "api", None)
    assert _section(root, "api") == api[:-1]


def test_set_lines_refuses_a_second_name_in_the_readers_words_and_writes_nothing(world):
    # spec 14.1 (one gr2-set line per member): the writer refuses a second name with the sentence the reader uses
    root = world["root"]
    git(root, "config", "-f", str(root / ".gitmodules"), "--add", "submodule.api.gr2-set", "old")
    before = _root_state(root)
    with pytest.raises(griplink.GriplinkRefusal) as exc:
        griplink._set_lines(root, "api", ["b", "a"])
    assert _root_state(root) == before
    assert _section(root, "api")[-1] == "gr2-set = old"
    assert exc.value.code == 4 and str(exc.value).startswith(
        f"member 'api': {declared_sets.ONE_SET_PER_MEMBER}, so it takes one set name, got ['b', 'a']"), str(exc.value)


@pytest.mark.parametrize("bad", ["c#d", "a b", "x;y", "", "é", 7])
def test_set_lines_refuses_a_name_outside_the_set_charset_and_writes_nothing(world, bad):
    root = world["root"]
    # api already has a line, so a refusal that came after the old lines were unset would lose it
    git(root, "config", "-f", str(root / ".gitmodules"), "--add", "submodule.api.gr2-set", "old")
    before = _root_state(root)
    with pytest.raises(griplink.GriplinkRefusal) as exc:
        griplink._set_lines(root, "api", bad)
    assert _root_state(root) == before
    assert _section(root, "api")[-1] == "gr2-set = old"
    assert exc.value.code == 4 and str(exc.value).startswith(f"{bad!r}: not a set name gr2 takes"), str(exc.value)


@pytest.mark.parametrize("bad", ["a b", "..", "-x"])
def test_set_lines_refuses_a_member_name_gr2_does_not_take_and_writes_nothing(world, bad):
    # The member name is spliced into the config key submodule.<m>.gr2-set, so it is checked before any write.
    root = world["root"]
    before = _root_state(root)
    with pytest.raises(griplink.GriplinkRefusal) as exc:
        griplink._set_lines(root, bad, "ok")
    assert _root_state(root) == before
    assert exc.value.code == 3 and str(exc.value).startswith(f"{bad!r}: not a member name gr2 takes"), str(exc.value)


def test_add_of_a_git_directory_under_the_root_refuses_and_names_git_add(world, monkeypatch):
    root = world["root"]
    git(root, "init", "-q", "tools")  # a git directory under the root, not a member
    before = _root_state(root)
    r = gr2(root, monkeypatch, "add", "tools")
    assert r.exit_code == 2, (r.exit_code, r.stderr)
    assert r.stderr.startswith(f"gr2 add: tools is a path inside this workspace; to stage it: git -C {root} add "
                               "tools; "), r.stderr
    assert not (root / "tools.griplink").exists()
    assert _root_state(root) == before


def test_add_of_a_file_inside_a_member_refuses_naming_that_member(world, monkeypatch):
    root = world["root"]
    before = _root_state(root)
    r = gr2(root, monkeypatch, "add", "api/a.txt")
    assert r.exit_code == 2, (r.exit_code, r.stderr)
    assert f"to stage it: git -C {root / 'api'} add a.txt; " in r.stderr, r.stderr
    assert _root_state(root) == before


def test_add_of_a_local_repository_outside_the_root_still_adds(world, monkeypatch):
    root = world["root"]
    other = world["tmp"] / "src-api"  # a local repository outside the root: the control
    r = gr2(root, monkeypatch, "add", str(other), "api2")
    assert r.exit_code == 0, r.stderr
    assert git(root, "ls-tree", "HEAD", "api2.griplink/001.2_pin").startswith("160000 commit ")


def test_add_of_the_member_checkout_itself_prints_a_command_that_stages_that_checkout(world, monkeypatch):
    import shlex

    root = world["root"]
    r = gr2(root, monkeypatch, "add", "api")
    assert r.exit_code == 2, (r.exit_code, r.stderr)
    command = r.stderr.split("to stage it: ", 1)[1].split(";", 1)[0]
    assert shlex.split(command) == ["git", "-C", str(root / "api"), "add", "."], command


def test_a_git_failure_line_masks_a_credential_git_echoed(world, monkeypatch):
    from gr2.python_cli import gitops

    secret = "s3cr3tpassw0rd"

    def failing_out(repo, *args, **kwargs):
        raise gitops.GitError(tuple(args), 128, f"fatal: unable to access 'https://alice:{secret}@example.invalid/x.git/'",
                              frozenset({secret}))

    monkeypatch.setattr(gitops, "out", failing_out)
    r = gr2(world["root"], monkeypatch, "add", f"https://alice:{secret}@example.invalid/x.git", "x")
    assert r.exit_code == 3, (r.exit_code, r.stderr)
    assert r.stderr.startswith("gr2 add: git failed: ") and secret not in r.stdout + r.stderr, r.stderr

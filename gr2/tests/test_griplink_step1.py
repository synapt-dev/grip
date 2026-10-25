"""Full mode on the griplink root, step 1: commit, push and checkout, called as functions.

The proof, counted independently against the prototype: one change across two members on
three paths lands through two calls (commit -a, then push), and a stranger who clones the root, fetches the names
and checks out sees all three paths. The member's own git does not move.
"""
from __future__ import annotations

import os
import subprocess
import unicodedata
from pathlib import Path

import pytest

from gr2.python_cli import griplink
from gr2.python_cli.griplink import GriplinkRefusal

ENV = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict:
    """A workspace with members api and web (three files each), pushed once to a bare root remote."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for x in ("api", "web"):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(tmp_path / f"remote-{x}.git"))
        src = tmp_path / f"src-{x}"
        (src / "src").mkdir(parents=True)
        git(src, "init", "-q", "-b", "main")
        for j in (1, 2, 3):
            (src / "src" / f"f{j}.py").write_text(f"line {j} of {x}\n")
        git(src, "add", ".")
        git(src, "commit", "-q", "-m", f"{x}: v1")
        git(src, "push", "-q", str(tmp_path / f"remote-{x}.git"), "HEAD:main")
    ws = tmp_path / "ws"
    griplink.init(ws)
    griplink.add(ws, "api", str(tmp_path / "remote-api.git"))
    griplink.add(ws, "web", str(tmp_path / "remote-web.git"))
    remote = tmp_path / "root.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(ws, "remote", "add", "origin", str(remote))
    griplink.push(ws)
    return {"tmp": tmp_path, "ws": ws, "remote": remote}


def _edits(ws: Path) -> None:
    (ws / "api" / "src" / "f1.py").write_text("api change 1\n")
    (ws / "api" / "src" / "f2.py").write_text("api change 2\n")
    (ws / "web" / "src" / "f3.py").write_text("web change 3\n")


def _landed(w: dict) -> int:
    """How many of the three paths a stranger sees with this change's content."""
    s = w["tmp"] / "stranger"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{w['remote']}", str(s)], check=True, capture_output=True)
    git(s, "fetch", "-q", "origin", f"+{griplink.NS}/*:{griplink.NS}/*")
    griplink.checkout(s)
    want = {("api", "src/f1.py"): "api change 1\n", ("api", "src/f2.py"): "api change 2\n",
            ("web", "src/f3.py"): "web change 3\n"}
    got = 0
    for (m, p), text in want.items():
        f = griplink.member_dir(s, m) / p
        got += f.exists() and f.read_text() == text
    return got


def test_commit_a_then_push_lands_all_three_paths_and_leaves_member_git(world):
    ws, remote = world["ws"], world["remote"]
    _edits(ws)
    before_remote = git(remote, "rev-parse", "main")
    member_head = git(griplink.member_dir(ws, "api"), "rev-parse", "HEAD")
    got = griplink.commit(ws, "change", stage_all=True)
    assert sorted(got["moved"]) == ["api", "web"], got
    assert git(remote, "rev-parse", "main") == before_remote, "commit -a pushed"
    griplink.push(ws)
    assert _landed(world) == 3
    assert git(griplink.member_dir(ws, "api"), "rev-parse", "HEAD") == member_head, "member git moved before promote"


def test_commit_with_nothing_staged_refuses_in_gits_words_and_cuts_nothing(world):
    ws = world["ws"]
    _edits(ws)
    head = git(ws, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(ws, "change")
    assert exc.value.code == 1
    assert "no changes added to commit" in str(exc.value), str(exc.value)
    assert git(ws, "rev-parse", "HEAD") == head


def test_push_refuses_fetch_first_when_the_remote_shadow_moved(world):
    ws, remote = world["ws"], world["remote"]
    tip = git(ws, "rev-parse", "HEAD")
    other = git(ws, "commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", "another writer")
    git(ws, "push", "-q", str(remote), f"{other}:refs/heads/main")
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("fetch first:"), str(exc.value)
    assert git(remote, "rev-parse", "main") == other, "something was pushed"


def test_push_refuses_a_member_rewind_by_entry_and_allow_rewind_takes_it(world):
    ws, remote = world["ws"], world["remote"]
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    griplink.push(ws)
    pushed = git(ws, "rev-parse", griplink.member_ref("api"))
    # a new api chain that does not descend from what the remote shadow recorded
    orphan = git(ws, "commit-tree", f"{pushed}^{{tree}}", "-m", "rewritten")
    git(ws, "update-ref", griplink.member_ref("api"), orphan)
    (ws / "web" / "src" / "f1.py").write_text("web change 1\n")
    griplink.commit(ws, "web again", stage_all=True)
    remote_main = git(remote, "rev-parse", "main")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: "), str(exc.value)
    assert "--allow-rewind" in str(exc.value)
    assert git(remote, "rev-parse", "main") == remote_main, "something was pushed"
    griplink.push(ws, allow_rewind=True)
    assert git(remote, "rev-parse", griplink.member_ref("api")) == orphan
    tip = git(ws, "rev-parse", "HEAD")
    record = git(remote, "log", "-1", "--format=%B", griplink.rewound_ref(tip))
    assert f"api: rewind: {pushed} -> {orphan}" in record, record


def test_a_push_with_nothing_rewound_writes_no_rewind_record(world):
    ws, remote = world["ws"], world["remote"]
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    griplink.push(ws, allow_rewind=True)
    assert git(remote, "for-each-ref", f"{griplink.NS}/rewound/") == ""


@pytest.mark.parametrize("verb", ["commit", "push"])
def test_a_second_root_branch_is_refused_by_name(world, verb):
    ws = world["ws"]
    git(ws, "checkout", "-q", "-b", "topic")
    _edits(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(ws, "change", stage_all=True) if verb == "commit" else griplink.push(ws)
    assert exc.value.code == 3
    assert str(exc.value) == "topic: full mode carries one root branch for now; more than one comes later", str(exc.value)


def _refusing_hook(remote: Path, text: str) -> None:
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\ncat >&2 <<'EOF'\n{text}\nEOF\nexit 1\n")
    hook.chmod(0o755)


def test_a_size_refusal_prints_the_sentence_then_gits_own_words_at_exit_5(world):
    ws, remote = world["ws"], world["remote"]
    _refusing_hook(remote, "error: File src/f1.py is 101.00 MB; this exceeds GitHub's file size limit of 100.00 MB")
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    first, rest = str(exc.value).split("\n", 1)
    assert exc.value.code == 5
    assert first == ("push refused by the host for size: src/f1.py in a member not determined is 101.00 MB "
                     "(host limit 100.00 MB); nothing moved")
    assert "remote: error: File src/f1.py is 101.00 MB; this exceeds GitHub's file size limit of 100.00 MB" in rest, rest


def test_any_other_refusal_prints_gits_words_alone_at_exit_5(world):
    ws, remote = world["ws"], world["remote"]
    _refusing_hook(remote, "main is closed for now")
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 5
    assert "the host refused" not in str(exc.value)
    assert "remote: main is closed for now" in str(exc.value), str(exc.value)


# ---- the prototype's rows, by their own names (T1, T1b, T2, T4, T9, T10; status lines are step 2) ---------------
NS = griplink.NS


def _blob_at(repo: Path, spec: str) -> str | None:
    p = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", spec], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def _parents(repo: Path, rev: str = "HEAD") -> list[str]:
    return git(repo, "rev-list", "--parents", "-n1", rev).split()[1:]


def _stage(member: Path, path: str, text: str) -> None:
    (member / path).parent.mkdir(parents=True, exist_ok=True)
    (member / path).write_text(text)
    git(member, "add", path)


def test_t1_one_field_per_entry_the_stook_follows_a_branch_and_main_is_the_shadow(world):
    ws = world["ws"]
    assert git(ws, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _blob_at(ws, f"{NS}/STOOK:api.griplink/002.2_head") == f"ref: {NS}/__members__/api/STOOK"
    assert _blob_at(ws, "HEAD:api.griplink/002.2_head") is None, "no workspace change yet, so no head in the shadow"
    assert git(ws, "status", "--porcelain") == ""
    assert git(ws, "rev-parse", f"{NS}/SHADOW") == git(ws, "rev-parse", "main")


def test_t1b_commit_with_nothing_staged_refuses_exit_1_and_makes_no_commit(world):
    ws = world["ws"]
    before = git(ws, "rev-parse", "main", f"{NS}/STOOK")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(ws, "nothing")
    assert exc.value.code == 1 and str(exc.value).splitlines()[-1].startswith("nothing to commit"), str(exc.value)
    assert git(ws, "rev-parse", "main", f"{NS}/STOOK") == before


def test_t2_a_member_change_stays_in_the_workspace_and_commit_cuts_stook_and_shadow(world):
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    pin0 = git(api, "rev-parse", "HEAD")
    _stage(api, "src/f1.py", "def login(): return mfa()\n")
    griplink.commit(ws, "api: mfa")
    tip = git(ws, "rev-parse", f"{NS}/__members__/api/STOOK")
    assert git(api, "rev-parse", "HEAD") == pin0, "the member's own git moved"
    assert _blob_at(ws, "HEAD:api.griplink/002.2_head") == tip
    assert _blob_at(ws, f"{NS}/STOOK:api.griplink/002.2_head") == f"ref: {NS}/__members__/api/STOOK"
    for rev in ("HEAD", f"{NS}/STOOK"):
        names = git(ws, "ls-tree", "-r", "--name-only", rev).splitlines()
        assert not [n for n in names if "002.2_ref" in n or "003.2_sha" in n], names
    assert tip in _parents(ws), "the resolved tip is not a parent of the shadow"
    assert git(ws, "status", "--porcelain", "--ignore-submodules=dirty") == ""
    _stage(api, "src/mfa.py", "def mfa(): return True\n")
    griplink.commit(ws, "api: helper")
    assert git(ws, "rev-list", "--count", f"{NS}/__members__/api/STOOK") == "2"


def test_t4_a_strangers_plain_clone_gets_every_resolved_object_and_checkout_stages_it(world):
    ws, remote = world["ws"], world["remote"]
    api = griplink.member_dir(ws, "api")
    _stage(api, "src/f1.py", "def login(): return mfa()\n")
    _stage(api, "src/mfa.py", "def mfa(): return True\n")
    griplink.commit(ws, "api: mfa")
    griplink.push(ws)
    assert git(remote, "rev-parse", f"{NS}/SHADOW") == git(remote, "rev-parse", "main")
    s = world["tmp"] / "stranger"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{remote}", str(s)], check=True, capture_output=True)
    assert git(s, "for-each-ref", NS) == "", "a plain clone fetched sub refs"
    heads = [line.split()[2] for line in git(s, "ls-tree", "-r", "HEAD").splitlines() if line.endswith("002.2_head")]
    assert heads and all(subprocess.run(["git", "-C", str(s), "cat-file", "-e", _blob_at(s, h)]).returncode == 0
                         for h in heads), "a sha the shadow names is missing from the plain clone"
    griplink.checkout(s)
    staged = git(griplink.member_dir(s, "api"), "diff", "--cached", "--name-only").splitlines()
    assert staged == ["src/f1.py", "src/mfa.py"], staged


def test_t9_like_git_commit_a_takes_tracked_changes_only_and_does_not_push(world):
    ws, remote = world["ws"], world["remote"]
    web = griplink.member_dir(ws, "web")
    (web / "src" / "f2.py").write_text("web, edited, not added\n")
    (web / "src" / "new.py").write_text("brand new, untracked\n")
    before = git(ws, "rev-parse", "main")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(ws, "web: f2")
    assert exc.value.code == 1 and "web: changes not staged for commit: src/f2.py" in str(exc.value)
    assert git(ws, "rev-parse", "main") == before
    wpin, remote0 = git(web, "rev-parse", "HEAD"), git(remote, "rev-parse", "main")
    griplink.commit(ws, "web: f2", stage_all=True)
    names = git(ws, "ls-tree", "-r", "--name-only", f"{NS}/__members__/web/STOOK").splitlines()
    assert any(n.endswith("overlay/src/f2.py") for n in names) and not any(n.endswith("overlay/src/new.py") for n in names)
    assert git(web, "rev-parse", "HEAD") == wpin
    assert git(remote, "rev-parse", "main") == remote0, "commit pushed"
    griplink.push(ws)
    assert git(remote, "rev-parse", "main") == git(ws, "rev-parse", "main")


def test_t10_plain_git_status_at_the_root_reads_clean_and_a_member_commit_still_shows(world):
    ws = world["ws"]
    web = griplink.member_dir(ws, "web")
    for m in ("api", "web"):
        assert git(ws, "config", "-f", ".gitmodules", f"submodule.{m}.ignore") == "dirty"
    _stage(web, "src/f1.py", "web change\n")
    griplink.commit(ws, "web: change")
    assert git(web, "diff", "--cached", "--name-only") != "" and git(ws, "status", "--porcelain") == ""
    (web / "src" / "f3.py").write_text("unstaged, for T10\n")
    assert git(ws, "status", "--porcelain") == "", "an unstaged member edit showed in root git status"
    assert griplink.unstaged(ws) == {"web": ["src/f3.py"]}
    git(web, "checkout", "-q", "--", "src/f3.py")
    git(web, "commit", "-q", "--allow-empty", "-m", "a new member commit")
    assert "web.griplink/001.2_pin" in git(ws, "status", "--porcelain")


# ---- a root from someone else: checkout writes nothing a remote can aim outside the member -------------------------
# The planted rows build a GripCommit with the module's own helpers, the way a remote could.

def _planted(w: dict, overlay: dict, deleted: dict | None = None) -> Path:
    """Cut a GripCommit carrying `overlay` for member api, push it, and return a stranger's root (names fetched)."""
    ws = w["ws"]
    pin = git(ws, "rev-parse", "HEAD:api.griplink/001.2_pin")
    ov = {p: (mode, "blob", griplink._blob(ws, text)) for p, (mode, text) in overlay.items()}
    gt = griplink._gripcommit_tree(ws, pin, ov, deleted or {}, "planted")
    gc = git(ws, "commit-tree", gt, "-m", "planted")
    git(ws, "update-ref", griplink.member_ref("api"), gc)
    griplink._cut(ws, "planted", [git(ws, "rev-parse", "HEAD")])
    griplink.push(ws)
    s = w["tmp"] / "stranger"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{w['remote']}", str(s)], check=True, capture_output=True)
    git(s, "fetch", "-q", "origin", f"+{griplink.NS}/*:{griplink.NS}/*")
    return s


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-commit", ".GIT/config", "src/../../x", "../x"])
def test_checkout_refuses_a_change_aimed_at_git_or_outside_the_member(world, path, monkeypatch):
    mark = world["tmp"] / "PWNED"
    monkeypatch.setenv("PWN_MARK", str(mark))
    evil = '[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfsmonitor = "touch \\"$PWN_MARK\\"; false"\n'
    s = _planted(world, {path: ("100755", evil)})
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(s)
    assert exc.value.code == 3 and str(exc.value).startswith("api: the workspace change names a path"), str(exc.value)
    assert not (s / "api.griplink" / PIN_DIR / ".git").exists() and not (s / "web.griplink" / PIN_DIR / ".git").exists()
    assert not (s / "x").exists() and not (s / "api.griplink" / "x").exists() and not mark.exists()


PIN_DIR = "001.2_pin"


def test_checkout_writes_nothing_through_a_member_symlink(world, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src-api"
    (src / "lnk").symlink_to(outside)
    git(src, "add", "lnk")
    git(src, "commit", "-q", "-m", "a link")
    git(src, "push", "-q", str(tmp_path / "remote-api.git"), "HEAD:main")
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    git(api, "pull", "-q", "--ff-only", "origin", "main")
    git(ws, "add", "api.griplink/001.2_pin")
    git(ws, "commit", "-q", "-m", "api pin moves")
    s = _planted(world, {"lnk/planted": ("100644", "through the link\n")})
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(s)
    assert exc.value.code == 3 and "lnk/planted" in str(exc.value), str(exc.value)
    assert not (outside / "planted").exists()


def test_checkout_refuses_a_member_with_uncommitted_work_and_moves_nothing(world):
    ws = world["ws"]
    api, web = griplink.member_dir(ws, "api"), griplink.member_dir(ws, "web")
    (api / "src" / "f1.py").write_text("my unsaved work\n")
    web_head = git(web, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: uncommitted work in src/f1.py"), str(exc.value)
    assert (api / "src" / "f1.py").read_text() == "my unsaved work\n"
    assert git(web, "rev-parse", "HEAD") == web_head


def test_checkout_refuses_a_gitmodules_url_shaped_like_an_option(world, monkeypatch):
    mark = world["tmp"] / "PWNED"
    monkeypatch.setenv("PWN_MARK", str(mark))
    ws = world["ws"]
    git(ws, "config", "-f", ".gitmodules", "submodule.web.url", '--upload-pack=sh -c "touch $PWN_MARK"')
    git(ws, "add", ".gitmodules")
    griplink._cut(ws, "url", [git(ws, "rev-parse", "HEAD")])
    griplink.push(ws)
    s = world["tmp"] / "stranger"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{world['remote']}", str(s)], check=True, capture_output=True)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(s)
    assert exc.value.code == 3 and str(exc.value).startswith("web: the url"), str(exc.value)
    assert not mark.exists() and not (s / "api.griplink" / PIN_DIR / ".git").exists()


@pytest.mark.parametrize("name", ["a/b", "..", "-x", ".x", "a..b", ""])
def test_add_refuses_a_member_name_that_is_not_plain(world, name):
    ws = world["ws"]
    before = git(ws, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.add(ws, name, str(world["tmp"] / "remote-api.git"))
    assert exc.value.code == 3 and "not a member name" in str(exc.value), str(exc.value)
    assert git(ws, "rev-parse", "HEAD") == before


def test_add_refuses_a_url_shaped_like_an_option(world):
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.add(world["ws"], "third", "--upload-pack=touch x")
    assert exc.value.code == 3 and str(exc.value).startswith("third: the url"), str(exc.value)
    assert not griplink.member_dir(world["ws"], "third").exists()


def test_push_refuses_at_exit_5_when_the_remote_cannot_be_read(world):
    ws = world["ws"]
    git(ws, "remote", "set-url", "origin", str(world["tmp"] / "no-such-remote.git"))
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 5 and str(exc.value).startswith("cannot read the remote shadow from origin"), str(exc.value)


def test_push_to_a_remote_with_only_a_lookalike_main_is_a_first_push_and_leaves_the_lookalike(world):
    ws, remote = world["ws"], world["remote"]
    old = git(remote, "rev-parse", "refs/heads/main")
    git(remote, "update-ref", "refs/a/refs/heads/main", old)
    for ref in ("refs/heads/main", griplink.SHADOW):
        git(remote, "update-ref", "-d", ref)
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    griplink.push(ws)
    assert git(remote, "rev-parse", "refs/heads/main") == git(ws, "rev-parse", "HEAD") != old
    assert git(remote, "rev-parse", "refs/a/refs/heads/main") == old


def test_push_beside_a_lookalike_main_pushes_as_with_the_real_main_alone(world):
    ws, remote = world["ws"], world["remote"]
    old = git(remote, "rev-parse", "refs/heads/main")
    git(remote, "update-ref", "refs/a/refs/heads/main", old)
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    griplink.push(ws)
    assert git(remote, "rev-parse", "refs/heads/main") == git(ws, "rev-parse", "HEAD") != old
    assert git(remote, "rev-parse", "refs/a/refs/heads/main") == old


def test_push_names_a_malformed_listing_as_unreadable_not_as_two_mains(world, monkeypatch):
    ws, remote = world["ws"], world["remote"]
    before = git(remote, "for-each-ref")
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    _malformed_ls_remote(monkeypatch)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 5
    assert str(exc.value) == ("cannot read the remote shadow from origin: a row of its listing is malformed; "
                              "nothing was pushed")
    assert git(remote, "for-each-ref") == before


def test_push_at_a_root_with_no_remote_names_the_remote_to_add(world):
    ws = world["ws"]
    git(ws, "remote", "remove", "origin")
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    refs = git(ws, "for-each-ref")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert git(ws, "for-each-ref") == refs
    msg = str(exc.value)
    assert exc.value.code == 5 and msg.startswith("origin is not a remote of this root; nothing was pushed"), msg
    assert "next: git remote add origin <url> at the root, then gr2 push again" in msg, msg


def test_pull_at_a_root_with_no_remote_names_the_remote_to_add(world):
    ws = world["ws"]
    git(ws, "remote", "remove", "origin")
    refs = git(ws, "for-each-ref")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert git(ws, "for-each-ref") == refs
    msg = str(exc.value)
    assert exc.value.code == 5 and msg.startswith("origin is not a remote of this root; nothing was pulled"), msg
    assert "next: git remote add origin <url> at the root, then gr2 pull again" in msg, msg


def test_push_refuses_at_exit_5_when_the_fetch_fails(world, monkeypatch):
    ws = world["ws"]
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    real = griplink.gitops.run

    def failing_fetch(repo, *args, **kw):
        if args and args[0] == "fetch":
            return subprocess.CompletedProcess(args, 128, "", "fatal: the fetch failed")
        return real(repo, *args, **kw)

    monkeypatch.setattr(griplink.gitops, "run", failing_fetch)
    before = git(world["remote"], "rev-parse", "main")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.push(ws)
    assert exc.value.code == 5 and "fatal: the fetch failed" in str(exc.value), str(exc.value)
    assert git(world["remote"], "rev-parse", "main") == before


def test_checkout_refuses_a_deletion_aimed_outside_the_member(world):
    griplink._blob(world["ws"], b"")
    s = _planted(world, {}, {"../keep": ("100644", "blob", griplink.EMPTY_BLOB)})
    keep = s / "api.griplink" / "keep"
    keep.write_text("not the member's to delete\n")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(s)
    assert exc.value.code == 3 and str(exc.value).startswith("api: the workspace change names a path"), str(exc.value)
    assert keep.read_text() == "not the member's to delete\n"


def test_checkout_refuses_an_untracked_file_the_change_would_overwrite(world):
    # The author's own root, an untracked file at a path the incoming change adds.
    _planted(world, {"src/mine.py": ("100644", "theirs\n")})
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    (api / "src" / "mine.py").write_text("my untracked work\n")
    head = git(api, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: untracked files would be overwritten: src/mine.py"), \
        str(exc.value)
    assert (api / "src" / "mine.py").read_text() == "my untracked work\n"
    assert git(api, "rev-parse", "HEAD") == head


def test_checkout_refuses_an_untracked_file_at_a_path_the_pin_tracks(world, tmp_path):
    src = tmp_path / "src-api"
    (src / "src" / "new.py").write_text("tracked at the new pin\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "api: new.py")
    git(src, "push", "-q", str(tmp_path / "remote-api.git"), "HEAD:main")
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    git(api, "pull", "-q", "--ff-only", "origin", "main")
    git(ws, "add", "api.griplink/001.2_pin")
    git(ws, "commit", "-q", "-m", "api pin moves")
    git(api, "checkout", "-q", "--detach", "HEAD~1")
    (api / "src" / "new.py").write_text("mine, untracked here\n")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and "untracked files would be overwritten: src/new.py" in str(exc.value), str(exc.value)
    assert (api / "src" / "new.py").read_text() == "mine, untracked here\n"


def test_checkout_refuses_an_untracked_case_twin_of_a_path_the_change_adds(world):
    # On a case-folding filesystem NEW.txt and new.txt are one file; plain git checkout refuses this too.
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    ic = subprocess.run(["git", "-C", str(api), "config", "--bool", "core.ignorecase"], capture_output=True, text=True)
    if ic.stdout.strip() != "true":
        pytest.skip("needs a case-insensitive filesystem (core.ignorecase false here)")
    _planted(world, {"new.txt": ("100644", "theirs\n")})
    (api / "NEW.txt").write_text("mine\n")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and "untracked files would be overwritten: NEW.txt" in str(exc.value), str(exc.value)
    assert (api / "NEW.txt").read_text() == "mine\n"


@pytest.mark.parametrize("ignorecase", ["as cloned", "false"])
def test_checkout_refuses_an_untracked_normalization_twin_of_a_path_the_change_adds(world, ignorecase):
    # On a normalizing filesystem the NFC and NFD spellings of a name are one file; plain git checkout refuses this too.
    # "false" stands for a case-sensitive volume that still normalizes (core.precomposeunicode alone).
    nfc, nfd = unicodedata.normalize("NFC", "café.txt"), unicodedata.normalize("NFD", "café.txt")
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    if ignorecase == "false":
        git(api, "config", "core.ignorecase", "false")
    (api / nfc).write_text("mine\n")
    if not (api / nfd).exists():
        (api / nfc).unlink()
        pytest.skip("needs a filesystem that treats NFC and NFD spellings as one name")
    (api / nfc).unlink()
    _planted(world, {nfd: ("100644", "theirs\n")})
    (api / nfc).write_text("mine\n")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and "untracked files would be overwritten: " in str(exc.value), str(exc.value)
    assert (api / nfc).read_text() == "mine\n"


def test_a_case_folded_name_for_a_member_symlink_writes_nothing_outside(world, tmp_path):
    # `LNK/planted` against a tracked `lnk -> outside`. Whatever git does with the case, nothing lands
    # outside the member.
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src-api"
    (src / "lnk").symlink_to(outside)
    git(src, "add", "lnk")
    git(src, "commit", "-q", "-m", "a link")
    git(src, "push", "-q", str(tmp_path / "remote-api.git"), "HEAD:main")
    ws = world["ws"]
    git(griplink.member_dir(ws, "api"), "pull", "-q", "--ff-only", "origin", "main")
    git(ws, "add", "api.griplink/001.2_pin")
    git(ws, "commit", "-q", "-m", "api pin moves")
    s = _planted(world, {"LNK/planted": ("100644", "case-folded\n")})
    try:
        griplink.checkout(s)
    except GriplinkRefusal as exc:
        assert exc.code == 3, str(exc)
    assert list(outside.iterdir()) == []


def test_checkout_after_commit_on_the_same_root_loses_nothing_and_does_not_refuse(world):
    # commit leaves the member's own git alone, so the change stays staged there; checking out again
    # must not refuse work that is exactly the change it is about to apply.
    ws = world["ws"]
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    assert griplink.checkout(ws) == {"api": 2, "web": 1}
    assert (ws / "api" / "src" / "f1.py").read_text() == "api change 1\n"
    assert (ws / "web" / "src" / "f3.py").read_text() == "web change 3\n"


def test_an_edit_after_commit_still_refuses(world):
    ws = world["ws"]
    _edits(ws)
    griplink.commit(ws, "change", stage_all=True)
    (ws / "api" / "src" / "f1.py").write_text("edited after the commit\n")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: uncommitted work in src/f1.py"), str(exc.value)
    assert (ws / "api" / "src" / "f1.py").read_text() == "edited after the commit\n"


def test_checkout_refuses_a_member_checkout_that_lacks_the_pin(world):
    ws = world["ws"]
    missing = "ab" * 20
    git(ws, "update-index", "--cacheinfo", f"160000,{missing},api.griplink/001.2_pin")
    git(ws, "commit", "-q", "-m", "a pin the member does not hold")
    api = griplink.member_dir(ws, "api")
    head = git(api, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and str(exc.value).startswith(f"api: its checkout does not hold the pin {missing[:12]}"), \
        str(exc.value)
    assert git(api, "rev-parse", "HEAD") == head


def _malformed_ls_remote(monkeypatch):
    """ls-remote answers a line that is not a row; every other git call runs as it is."""
    real = griplink.gitops.run

    def fake(cwd, *args, **kw):
        if args[:1] == ("ls-remote",):
            return subprocess.CompletedProcess(["git", *args], 0, "not a row\n", "")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(griplink.gitops, "run", fake)

"""Full mode on the griplink root, step 5: promote to the member remotes (spec 13.8, Layne's five answers).

Push sends each member's promoted pin to the member's own remote before the root, so a plain
`git clone --recurse-submodules` of the root checks every member out at its pin.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import griplink
from gr2.python_cli.griplink import GriplinkRefusal

from tests.test_griplink_step1 import git, world  # noqa: F401  (api and web, pushed once)
from tests.test_griplink_step3 import _write


def _pin(root: Path, m: str, rev: str = "HEAD") -> str:
    return git(root, "rev-parse", f"{rev}:{m}.griplink/{griplink.PIN}")


def _remote_main(w: dict, m: str) -> str:
    return git(w["tmp"], "--git-dir", str(w["tmp"] / f"remote-{m}.git"), "rev-parse", "refs/heads/main")


def _branch_line(ws: Path, m: str, value: str, commit: bool = True) -> None:
    """The member's branch line; committed by default, since gr2 reads .gitmodules from the root's HEAD commit."""
    git(ws, "config", "-f", str(ws / ".gitmodules"), f"submodule.{m}.branch", value)
    if commit:
        git(ws, "add", ".gitmodules")
        git(ws, "commit", "-q", "-m", f"{m}: branch line")


def test_q3_an_uncommitted_branch_line_is_not_read(world):
    _branch_line(world["ws"], "api", "dev")
    _branch_line(world["ws"], "api", "other", commit=False)
    assert griplink.member_branch(world["ws"], "api") == "dev"


def test_q3_branch_is_main_when_gitmodules_names_none(world):
    assert griplink.member_branch(world["ws"], "api") == "main"


def test_q3_branch_is_the_gitmodules_line(world):
    _branch_line(world["ws"], "api", "dev")
    assert griplink.member_branch(world["ws"], "api") == "dev"
    assert griplink.member_branch(world["ws"], "web") == "main"


def test_q3_branch_dot_is_the_roots_own_branch_as_git_reads_it(world):
    _branch_line(world["ws"], "api", ".")
    assert griplink.member_branch(world["ws"], "api") == "main"


@pytest.mark.parametrize("bad", ["a..b", "-x", "has space", "x.lock", "a/"])
def test_q3_a_branch_line_git_would_not_take_refuses_by_member(world, bad):
    _branch_line(world["ws"], "api", bad)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.member_branch(world["ws"], "api")
    assert e.value.args[0].startswith(f"api: its .gitmodules branch line {bad!r} is not a branch name; nothing was")


def test_p1_one_push_lands_both_promoted_members_and_a_plain_recursive_clone_checks_them_out(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    _write(ws, "web", "src/f3.py", "web change 3\n")
    griplink.commit(ws, "api and web", stage_all=True)
    griplink.promote(ws, "api")
    griplink.promote(ws, "web")
    griplink.push(ws, push_members=True)
    assert _remote_main(world, "api") == _pin(ws, "api")
    assert _remote_main(world, "web") == _pin(ws, "web")
    s = world["tmp"] / "stranger"
    proc = subprocess.run(["git", "-c", "protocol.file.allow=always", "clone", "-q", "--recurse-submodules",
                           f"file://{world['remote']}", str(s)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for m in ("api", "web"):
        assert git(griplink.member_dir(s, m), "rev-parse", "HEAD") == _pin(ws, m)
    assert (griplink.member_dir(s, "api") / "src" / "f1.py").read_text() == "api change 1\n"
    assert (griplink.member_dir(s, "web") / "src" / "f3.py").read_text() == "web change 3\n"


def _other_pusher(w: dict, m: str, path: str, text: str) -> str:
    """Another desk commits `path` on the member remote's main; returns the new tip."""
    o = w["tmp"] / f"other-{m}"
    if not o.exists():
        subprocess.run(["git", "clone", "-q", str(w["tmp"] / f"remote-{m}.git"), str(o)], check=True)
    git(o, "pull", "-q", "--ff-only")
    (o / path).parent.mkdir(parents=True, exist_ok=True)
    (o / path).write_text(text)
    git(o, "add", "-A")
    git(o, "commit", "-q", "-m", f"other: {path}")
    git(o, "push", "-q", "origin", "HEAD:main")
    return git(o, "rev-parse", "HEAD")


def test_p2b_promote_lands_on_the_other_pushers_commit_with_both_changes(world):
    ws = world["ws"]
    other = _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    git(griplink.member_dir(ws, "api"), "fetch", "-q", "origin")
    got = griplink.promote(ws, "api")
    mrepo = griplink.member_dir(ws, "api")
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    assert git(mrepo, "show", f"{got['commit']}:src/f1.py") == "api change 1"
    assert git(mrepo, "show", f"{got['commit']}:src/f3.py") == "other desk's f3"
    assert _pin(ws, "api") == got["commit"]


def test_p3_a_same_line_conflict_refuses_by_member_and_path_and_moves_nothing(world):
    ws = world["ws"]
    _other_pusher(world, "api", "src/f1.py", "other desk's f1\n")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    mrepo = griplink.member_dir(ws, "api")
    git(mrepo, "fetch", "-q", "origin")
    pin0, head0, shadow0 = _pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")
    chain0 = git(ws, "rev-parse", griplink.member_ref("api"))
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: the change and main at ")
    assert "both changed src/f1.py; nothing was promoted" in e.value.args[0]
    assert (_pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")) == (pin0, head0, shadow0)
    assert git(ws, "rev-parse", griplink.member_ref("api")) == chain0


def test_p2b_unfetched_tracking_ref_promotes_on_the_pin_with_no_network(world):
    ws = world["ws"]
    _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")  # on the remote, never fetched here
    pin0 = _pin(ws, "api")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    got = griplink.promote(ws, "api")
    assert git(griplink.member_dir(ws, "api"), "rev-parse", f"{got['commit']}^@") == pin0


def _promote_both(ws: Path) -> None:
    _write(ws, "api", "src/f1.py", "api change 1\n")
    _write(ws, "web", "src/f3.py", "web change 3\n")
    griplink.commit(ws, "api and web", stage_all=True)
    griplink.promote(ws, "api")
    griplink.promote(ws, "web")


def _remote_tips(w: dict) -> tuple[str, str, str]:
    root_main = git(w["tmp"], "--git-dir", str(w["remote"]), "rev-parse", "refs/heads/main")
    return _remote_main(w, "api"), _remote_main(w, "web"), root_main


def test_p2_a_moved_member_remote_refuses_with_the_promote_step_and_pushes_nothing(world):
    ws = world["ws"]
    _promote_both(ws)  # promoted on the pin: the other desk's commit below was never fetched here
    _other_pusher(world, "api", "src/f2.py", "other desk's f2\n")
    before = _remote_tips(world)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.push(ws, push_members=True)
    assert e.value.args[0].startswith("api: the member remote moved (main is ")
    assert "next: gr2 promote api again, then push" in e.value.args[0]
    assert e.value.args[0].endswith("nothing was pushed")
    assert _remote_tips(world) == before  # web was not sent either, and the root did not move


def test_p4_a_rerun_re_sends_nothing(world):
    ws = world["ws"]
    _promote_both(ws)
    assert griplink.push(ws, push_members=True)["member_remotes"] == ["api", "web"]
    again = griplink.push(ws, push_members=True)
    assert again["member_remotes"] == [] and again["member_preflight"] == {}


def test_g1_a_member_hook_that_refuses_stops_the_push_before_any_member_or_the_root_moves(world):
    ws = world["ws"]
    _promote_both(ws)
    hook = griplink.member_dir(ws, "web") / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho 'web: this remote is gated' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = _remote_tips(world)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.push(ws, push_members=True)
    assert e.value.args[0].startswith("web: the dry run to its remote refused: ")
    assert "web: this remote is gated" in e.value.args[0]
    assert _remote_tips(world) == before  # api sorts first and was still not sent


def test_g2_a_member_with_no_hook_still_gets_the_dry_run_and_is_named(world, tmp_path, monkeypatch):
    ws = world["ws"]
    _promote_both(ws)
    log, shim = tmp_path / "dry.log", tmp_path / "shim"
    shim.mkdir()
    real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True, check=True).stdout.strip()
    (shim / "git").write_text(f'#!/bin/sh\nfor a in "$@"; do [ "$a" = --dry-run ] && echo "$*" >> {log}; done\n'
                              f'exec {real} "$@"\n')
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{__import__('os').environ['PATH']}")
    got = griplink.push(ws, push_members=True)
    assert got["member_preflight"] == {"api": "no pre-push hook", "web": "no pre-push hook"}
    dry = log.read_text().splitlines()
    assert len(dry) == 2
    assert [any(f"remote-{m}.git" in line for line in dry) for m in ("api", "web")] == [True, True]


def _second_desk(w: dict) -> Path:
    """A second workspace on the same root remote, pulled once so it carries the remote's chains."""
    ws2 = w["tmp"] / "ws2"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{w['remote']}", str(ws2)], check=True,
                   capture_output=True)
    griplink.pull(ws2)
    griplink.checkout(ws2)
    return ws2


def _remote_chain(w: dict, m: str) -> str:
    return git(w["tmp"], "ls-remote", str(w["remote"]), griplink.member_ref(m))


def test_p5_push_deletes_the_promoted_chain_and_the_other_desks_pull_drops_it(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.push(ws)
    assert _remote_chain(world, "api")  # the chain is on the root remote
    ws2 = _second_desk(world)
    assert git(ws2, "rev-parse", griplink.member_ref("api")) == git(ws, "rev-parse", griplink.member_ref("api"))
    griplink.promote(ws, "api")
    got = griplink.push(ws, push_members=True)
    assert got["chains_deleted"] == ["api"] and got["member_remotes"] == ["api"]
    assert _remote_chain(world, "api") == ""
    pulled = griplink.pull(ws2)
    assert pulled["dropped"] == ["api"]
    assert git(ws2, "for-each-ref", griplink.member_ref("api")) == ""
    assert _pin(ws2, "api") == _pin(ws, "api")


def test_p5b_a_chain_with_this_desks_commits_past_the_carried_tip_refuses_by_member(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.push(ws)
    ws2 = _second_desk(world)
    _write(ws2, "api", "src/f2.py", "desk two's f2\n")
    griplink.commit(ws2, "api f2 on desk two", stage_all=True)
    mine_chain, mine_head = git(ws2, "rev-parse", griplink.member_ref("api")), git(ws2, "rev-parse", "HEAD")
    griplink.promote(ws, "api")
    griplink.push(ws, push_members=True)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.pull(ws2)
    assert e.value.args[0].startswith("api: the incoming side promoted api's chain, and your chain has commits past ")
    assert "nothing was pulled. next: gr2 promote api to keep your work, then pull again" in e.value.args[0]
    assert (git(ws2, "rev-parse", griplink.member_ref("api")), git(ws2, "rev-parse", "HEAD")) == (mine_chain, mine_head)


def test_p6_pull_fetches_the_promoted_pin_from_the_member_remote_before_any_ref_moves(world):
    ws = world["ws"]
    ws2 = _second_desk(world)
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    new = griplink.promote(ws, "api")["commit"]
    griplink.push(ws, push_members=True)
    m2 = griplink.member_dir(ws2, "api")
    assert subprocess.run(["git", "-C", str(m2), "cat-file", "-e", f"{new}^{{commit}}"]).returncode != 0
    griplink.pull(ws2)
    assert _pin(ws2, "api") == new and git(m2, "rev-parse", "HEAD") == new
    assert (m2 / "src" / "f1.py").read_text() == "api change 1\n"


def test_p6_a_pin_its_member_remote_does_not_serve_refuses_by_member_and_moves_nothing(world):
    ws = world["ws"]
    ws2 = _second_desk(world)
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    new = griplink.promote(ws, "api")["commit"]
    griplink.push(ws)  # switch off: the root moves, the member remote never gets the pin
    head0 = git(ws2, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as e:
        griplink.pull(ws2)
    assert e.value.args[0].startswith(f"api: its remote does not serve {new[:12]}; nothing was pulled.")
    assert git(ws2, "rev-parse", "HEAD") == head0


def _url_line(ws: Path, m: str, url: str) -> None:
    git(ws, "config", "-f", str(ws / ".gitmodules"), f"submodule.{m}.url", url)


def test_p7_push_sends_to_the_url_head_names_never_an_uncommitted_edit(world):
    ws = world["ws"]
    _promote_both(ws)
    _url_line(ws, "api", str(world["tmp"] / "nowhere.git"))  # edited in the worktree, never committed
    got = griplink.push(ws, push_members=True)
    assert got["member_remotes"] == ["api", "web"]
    assert _remote_main(world, "api") == _pin(ws, "api")
    assert not (world["tmp"] / "nowhere.git").exists()


def test_p7b_a_relative_member_url_in_head_refuses_and_pushes_nothing(world):
    ws = world["ws"]
    _promote_both(ws)
    _url_line(ws, "api", "../remote-api.git")
    git(ws, "add", ".gitmodules")
    git(ws, "commit", "-q", "-m", "api: a relative url")
    before = _remote_tips(world)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.push(ws, push_members=True)
    assert e.value.args[0].startswith("api: its .gitmodules url '../remote-api.git' is relative, and gr2 pushes a "
                                      "member only to a url it can name; nothing was pushed")
    assert e.value.code == 3
    assert _remote_tips(world) == before


def _promoted_then_remote_moved(w: dict, path: str, text: str) -> tuple[Path, str, str]:
    """api's change promoted to a member commit (no chain left), then another desk pushes to api's remote and this
    desk fetches; returns (member checkout, the promoted commit, the other desk's tip)."""
    ws = w["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    promoted = griplink.promote(ws, "api")["commit"]
    other = _other_pusher(w, "api", path, text)
    mrepo = griplink.member_dir(ws, "api")
    git(mrepo, "fetch", "-q", "origin")
    return mrepo, promoted, other


def test_p8_promote_again_lands_an_already_promoted_commit_on_the_moved_tip(world, monkeypatch):
    ws = world["ws"]
    mrepo, promoted, other = _promoted_then_remote_moved(world, "src/f3.py", "other desk's f3\n")
    # whoever runs the second promote, and when, must not show: a commit made with the ambient identity and clock
    # would carry these, so the identity check below cannot pass by the two promotes sharing a second
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Second Promoter")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "second@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "@1600000000 +0000")
    got = griplink.promote(ws, "api")
    assert got["commit"] not in (None, promoted)
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    assert git(mrepo, "show", f"{got['commit']}:src/f1.py") == "api change 1"
    assert git(mrepo, "show", f"{got['commit']}:src/f3.py") == "other desk's f3"
    assert _pin(ws, "api") == got["commit"]
    who = "--format=%an%n%ae%n%ad%n%cn%n%ce%n%cd%n%B"
    assert git(mrepo, "log", "-1", "--date=raw", who, got["commit"]) == git(mrepo, "log", "-1", "--date=raw", who, promoted)


def test_p8b_a_same_line_conflict_after_promote_refuses_by_path_and_moves_nothing(world):
    ws = world["ws"]
    mrepo, promoted, _ = _promoted_then_remote_moved(world, "src/f1.py", "other desk's f1\n")
    pin0, head0, shadow0 = _pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")
    assert pin0 == promoted
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: the change and main at ")
    assert "both changed src/f1.py; nothing was promoted" in e.value.args[0]
    assert (_pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")) == (pin0, head0, shadow0)


def test_p8c_control_a_promoted_pin_the_remote_already_holds_has_nothing_to_promote(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.promote(ws, "api")
    griplink.push(ws, push_members=True)
    shadow0 = git(ws, "rev-parse", "HEAD")
    assert griplink.promote(ws, "api")["commit"] is None
    assert git(ws, "rev-parse", "HEAD") == shadow0


def test_p8d_control_an_unfetched_moved_remote_leaves_nothing_to_promote(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    promoted = griplink.promote(ws, "api")["commit"]
    _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")  # on the remote, never fetched here
    assert griplink.promote(ws, "api")["commit"] is None
    assert _pin(ws, "api") == promoted


def test_p8e_two_unpushed_promotes_land_as_one_commit_with_both_messages_oldest_first(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.promote(ws, "api")
    mrepo = griplink.member_dir(ws, "api")
    _write(ws, "api", "src/f4.py", "api change 4\n")
    git(mrepo, "add", "src/f4.py")  # a new file: commit -a stages tracked paths only, as git's does
    griplink.commit(ws, "api f4", stage_all=True)
    griplink.promote(ws, "api")
    other = _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")
    git(mrepo, "fetch", "-q", "origin")
    got = griplink.promote(ws, "api")
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    for path, text in (("src/f1.py", "api change 1"), ("src/f4.py", "api change 4"), ("src/f3.py", "other desk's f3")):
        assert git(mrepo, "show", f"{got['commit']}:{path}") == text
    body = git(mrepo, "log", "-1", "--format=%B", got["commit"])
    assert "api f1" in body and "api f4" in body and body.index("api f1") < body.index("api f4"), body


def test_p8f_commits_by_two_authors_refuse_by_member_and_move_nothing(world, monkeypatch):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.promote(ws, "api")
    mrepo = griplink.member_dir(ws, "api")
    _write(ws, "api", "src/f5.py", "by another author\n")
    git(mrepo, "add", "src/f5.py")
    with monkeypatch.context() as mp:  # the second change and its promote are another author's
        mp.setenv("GIT_AUTHOR_NAME", "Other Author")
        mp.setenv("GIT_AUTHOR_EMAIL", "other@example.invalid")
        griplink.commit(ws, "another author's change", stage_all=True)
    griplink.promote(ws, "api")
    _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")
    git(mrepo, "fetch", "-q", "origin")
    pin0, head0, shadow0 = _pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: the 2 commits main lacks carry 2 authors"), e.value.args[0]
    assert "git rebase origin/main, then gr2 commit -a, then gr2 promote api again" in e.value.args[0]
    assert e.value.code == 3
    assert (_pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")) == (pin0, head0, shadow0)


def test_p8h_a_change_the_moved_tip_already_holds_writes_no_commit_and_pins_the_tip(world):
    """The other desk lands the IDENTICAL change upstream (a rebase or squash merge). The 3-way tree equals the tip's,
    so promote again writes no commit (none whose tree equals its parent's) and the pin moves to the tip itself."""
    ws = world["ws"]
    mrepo, promoted, other = _promoted_then_remote_moved(world, "src/f1.py", "api change 1\n")
    got = griplink.promote(ws, "api")
    assert (got["commit"], got["paths"]) == (other, 0)
    assert _pin(ws, "api") == other
    assert git(mrepo, "rev-parse", "HEAD") == other
    assert git(mrepo, "rev-list", "--count", f"{other}..HEAD") == "0"


def test_p8i_two_authors_whose_change_the_tip_already_holds_pin_the_tip_and_refuse_nothing(world, monkeypatch):
    """Mixed authors refuse only when a commit is to be written (joining would drop one). When the moved tip already
    holds the whole change by content, nothing is joined, so the pin moves to the tip with no refusal."""
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.promote(ws, "api")
    mrepo = griplink.member_dir(ws, "api")
    _write(ws, "api", "src/f5.py", "by another author\n")
    git(mrepo, "add", "src/f5.py")
    with monkeypatch.context() as mp:
        mp.setenv("GIT_AUTHOR_NAME", "Other Author")
        mp.setenv("GIT_AUTHOR_EMAIL", "other@example.invalid")
        griplink.commit(ws, "another author's change", stage_all=True)
    griplink.promote(ws, "api")
    _other_pusher(world, "api", "src/f1.py", "api change 1\n")
    other = _other_pusher(world, "api", "src/f5.py", "by another author\n")
    git(mrepo, "fetch", "-q", "origin")
    got = griplink.promote(ws, "api")
    assert (got["commit"], got["paths"]) == (other, 0)
    assert _pin(ws, "api") == other


def _tracking_as_a_tag(mrepo: Path) -> str:
    """The member's tracking ref removed and a TAG of the same name put at its sha, as a member remote can carry one;
    returns that sha. git's rev-parse resolves refs/remotes/origin/main to refs/tags/refs/remotes/origin/main then."""
    sha = git(mrepo, "rev-parse", "refs/remotes/origin/main")
    git(mrepo, "update-ref", "-d", "refs/remotes/origin/main")
    git(mrepo, "update-ref", "refs/tags/refs/remotes/origin/main", sha)
    return sha


def test_p9_a_tag_named_like_the_tracking_ref_is_not_a_moved_tip_to_reland_on(world):
    ws = world["ws"]
    mrepo, promoted, other = _promoted_then_remote_moved(world, "src/f3.py", "other desk's f3\n")
    assert _tracking_as_a_tag(mrepo) == other
    shadow0 = git(ws, "rev-parse", "HEAD")
    assert griplink.promote(ws, "api")["commit"] is None
    assert (_pin(ws, "api"), git(ws, "rev-parse", "HEAD")) == (promoted, shadow0)


def test_p9b_a_change_promoted_with_only_the_lookalike_tag_lands_on_its_pin(world):
    ws = world["ws"]
    mrepo = griplink.member_dir(ws, "api")
    other = _other_pusher(world, "api", "src/f3.py", "other desk's f3\n")
    git(mrepo, "fetch", "-q", "origin")
    pin0 = _pin(ws, "api")
    assert _tracking_as_a_tag(mrepo) == other
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    got = griplink.promote(ws, "api")["commit"]
    assert git(mrepo, "rev-parse", f"{got}^@") == pin0


def _state(ws: Path, mrepo: Path) -> tuple[str, str, str]:
    return _pin(ws, "api"), git(mrepo, "rev-parse", "HEAD"), git(ws, "rev-parse", "HEAD")


def test_u1_relanding_over_an_untracked_file_the_moved_tip_adds_refuses_by_path_and_moves_nothing(world):
    ws = world["ws"]
    mrepo, _, _ = _promoted_then_remote_moved(world, "src/elsewhere.py", "upstream\n")
    (mrepo / "src" / "elsewhere.py").write_text("my local work\n")
    before = _state(ws, mrepo)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: promote would overwrite src/elsewhere.py in the member's checkout"), e.value.args[0]
    assert "nothing was promoted. next: move them aside, or add and commit them in the member, then gr2 promote api again" in e.value.args[0]
    assert e.value.code == 3
    assert (mrepo / "src" / "elsewhere.py").read_text() == "my local work\n"
    assert _state(ws, mrepo) == before


def test_u3_a_carried_change_landing_on_a_moved_tip_over_an_untracked_file_refuses(world):
    ws = world["ws"]
    mrepo = griplink.member_dir(ws, "api")
    _other_pusher(world, "api", "src/elsewhere.py", "upstream\n")
    git(mrepo, "fetch", "-q", "origin")
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    (mrepo / "src" / "elsewhere.py").write_text("my local work\n")
    before = _state(ws, mrepo)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: promote would overwrite src/elsewhere.py"), e.value.args[0]
    assert (mrepo / "src" / "elsewhere.py").read_text() == "my local work\n"
    assert _state(ws, mrepo) == before


def test_u4_an_ignored_file_at_a_path_the_moved_tip_adds_refuses_too(world):
    ws = world["ws"]
    mrepo, _, _ = _promoted_then_remote_moved(world, "build.log", "upstream log\n")
    with open(mrepo / ".git" / "info" / "exclude", "a") as f:
        f.write("build.log\n")
    (mrepo / "build.log").write_text("my ignored local file\n")
    assert git(mrepo, "status", "--porcelain", "--untracked-files=all") == ""  # git calls it ignored, not untracked
    before = _state(ws, mrepo)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: promote would overwrite build.log"), e.value.args[0]
    assert (mrepo / "build.log").read_text() == "my ignored local file\n"
    assert _state(ws, mrepo) == before


def test_u7_an_untracked_directory_where_the_moved_tip_adds_a_file_of_its_name_refuses(world):
    ws = world["ws"]
    mrepo, _, _ = _promoted_then_remote_moved(world, "docs", "upstream docs is a file\n")
    (mrepo / "docs").mkdir()
    (mrepo / "docs" / "x.txt").write_text("my untracked work in a directory\n")
    before = _state(ws, mrepo)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: promote would overwrite docs/x.txt"), e.value.args[0]
    assert e.value.code == 3
    assert (mrepo / "docs" / "x.txt").read_text() == "my untracked work in a directory\n"
    assert _state(ws, mrepo) == before

def test_u8_control_an_ignored_directory_the_tip_only_adds_beside_refuses_nothing(world):
    ws = world["ws"]
    mrepo, promoted, other = _promoted_then_remote_moved(world, "build/README", "upstream readme\n")
    with open(mrepo / ".git" / "info" / "exclude", "a") as f:
        f.write("build/\n")
    (mrepo / "build" / "sub").mkdir(parents=True)
    (mrepo / "build" / "sub" / "keep.txt").write_text("my ignored output\n")
    got = griplink.promote(ws, "api")
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    assert (mrepo / "build" / "sub" / "keep.txt").read_text() == "my ignored output\n"
    assert (mrepo / "build" / "README").read_text() == "upstream readme\n"


def test_u9_an_ignored_file_inside_an_ignored_directory_refuses_by_its_own_path(world):
    ws = world["ws"]
    mrepo, _, _ = _promoted_then_remote_moved(world, "build/out.txt", "upstream out\n")
    with open(mrepo / ".git" / "info" / "exclude", "a") as f:
        f.write("build/\n")
    (mrepo / "build").mkdir()
    (mrepo / "build" / "out.txt").write_text("my ignored output\n")
    (mrepo / "build" / "other.txt").write_text("also mine\n")
    before = _state(ws, mrepo)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: promote would overwrite build/out.txt in the member's checkout"), e.value.args[0]
    assert (mrepo / "build" / "out.txt").read_text() == "my ignored output\n"
    assert _state(ws, mrepo) == before

def test_u10_an_untracked_file_where_the_moved_tip_needs_a_directory_of_its_name_refuses(world):
    ws = world["ws"]
    mrepo, _, _ = _promoted_then_remote_moved(world, "vendor/lib.py", "upstream lib\n")
    (mrepo / "vendor").write_text("my untracked file named vendor\n")
    before = _state(ws, mrepo)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.promote(ws, "api")
    assert e.value.args[0].startswith("api: promote would overwrite vendor in the member's checkout"), e.value.args[0]
    assert (mrepo / "vendor").read_text() == "my untracked file named vendor\n"
    assert _state(ws, mrepo) == before

def test_u11_an_ignored_directory_the_tree_never_touches_is_never_read_file_by_file(world, monkeypatch):
    ws = world["ws"]
    mrepo, _, other = _promoted_then_remote_moved(world, "src/elsewhere.py", "upstream\n")
    with open(mrepo / ".git" / "info" / "exclude", "a") as f:
        f.write("node_modules/\n")
    (mrepo / "node_modules" / "pkg").mkdir(parents=True)
    (mrepo / "node_modules" / "pkg" / "index.js").write_text("big and ignored\n")
    calls = []
    real = griplink.gitops.out_bytes
    def spy(repo, *args, **kw):
        calls.append(args)
        return real(repo, *args, **kw)
    monkeypatch.setattr(griplink.gitops, "out_bytes", spy)
    got = griplink.promote(ws, "api")
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    assert any("--ignored=matching" in a for a in calls), calls  # the check ran
    assert not any("--ignored=traditional" in a for a in calls), calls

def test_u5_control_an_untracked_file_nothing_writes_survives_and_promote_proceeds(world):
    ws = world["ws"]
    mrepo, promoted, other = _promoted_then_remote_moved(world, "src/elsewhere.py", "upstream\n")
    (mrepo / "notes.txt").write_text("mine, nowhere in any tree\n")
    got = griplink.promote(ws, "api")
    assert got["commit"] not in (None, promoted)
    assert git(mrepo, "rev-parse", f"{got['commit']}^@") == other
    assert (mrepo / "notes.txt").read_text() == "mine, nowhere in any tree\n"


def test_c1_a_tip_holding_part_of_the_change_reports_the_commits_own_path_count(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    _write(ws, "api", "src/f2.py", "api change 2\n")
    git(griplink.member_dir(ws, "api"), "add", "src/f2.py")
    griplink.commit(ws, "api f1 and f2", stage_all=True)
    griplink.promote(ws, "api")
    _other_pusher(world, "api", "src/f1.py", "api change 1\n")  # the tip already holds f1, not f2
    mrepo = griplink.member_dir(ws, "api")
    git(mrepo, "fetch", "-q", "origin")
    got = griplink.promote(ws, "api")
    assert got["paths"] == 1, got
    assert git(mrepo, "diff", "--name-only", f"{got['commit']}^", got["commit"]) == "src/f2.py"


def test_u6_checkout_refuses_an_ignored_file_at_a_path_the_pin_tracks(world, tmp_path):
    src = tmp_path / "src-api"
    (src / "build.log").write_text("tracked at the new pin\n")
    git(src, "add", "-f", "build.log")
    git(src, "commit", "-q", "-m", "api: build.log")
    git(src, "push", "-q", str(tmp_path / "remote-api.git"), "HEAD:main")
    ws = world["ws"]
    api = griplink.member_dir(ws, "api")
    git(api, "pull", "-q", "--ff-only", "origin", "main")
    git(ws, "add", "api.griplink/001.2_pin")
    git(ws, "commit", "-q", "-m", "api pin moves")
    git(api, "checkout", "-q", "--detach", "HEAD~1")
    with open(api / ".git" / "info" / "exclude", "a") as f:
        f.write("build.log\n")
    (api / "build.log").write_text("mine, ignored here\n")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(ws)
    assert exc.value.code == 3 and "checkout would overwrite build.log" in str(exc.value), str(exc.value)
    assert (api / "build.log").read_text() == "mine, ignored here\n"


def _root_remote_refuses(w: dict) -> None:
    hook = w["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'root remote: refused by policy' >&2\nexit 1\n")
    hook.chmod(0o755)


def test_s1_a_root_push_refused_after_members_were_sent_names_the_members_sent(world):
    ws = world["ws"]
    _promote_both(ws)
    _root_remote_refuses(world)
    root_before = _remote_tips(world)[2]
    with pytest.raises(GriplinkRefusal) as e:
        griplink.push(ws, push_members=True)
    assert e.value.code == 5
    assert e.value.args[0].startswith("sent to their remotes: api, web\n"), e.value.args[0]
    assert "root remote: refused by policy" in e.value.args[0]
    assert _remote_tips(world) == (_pin(ws, "api"), _pin(ws, "web"), root_before)  # members sent, root unmoved


def test_s2_control_a_root_push_refused_with_no_member_sent_names_none(world):
    ws = world["ws"]
    _promote_both(ws)
    _root_remote_refuses(world)
    before = _remote_tips(world)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.push(ws)  # push_members off: no member is sent
    assert e.value.code == 5
    assert "sent to their remotes" not in e.value.args[0], e.value.args[0]
    assert "root remote: refused by policy" in e.value.args[0]
    assert _remote_tips(world) == before


def test_s3_the_prefix_names_only_the_members_this_push_sent(world):
    ws = world["ws"]
    _write(ws, "api", "src/f1.py", "api change 1\n")
    griplink.commit(ws, "api f1", stage_all=True)
    griplink.promote(ws, "api")
    assert _remote_main(world, "web") == _pin(ws, "web")  # web never changed: its remote already holds its pin
    _root_remote_refuses(world)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.push(ws, push_members=True)  # api is sent; web is not
    assert e.value.args[0].startswith("sent to their remotes: api\n"), e.value.args[0]
    assert _remote_main(world, "api") == _pin(ws, "api")

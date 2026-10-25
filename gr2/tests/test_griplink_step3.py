"""Full mode on the griplink root, step 3: pull, called as a function.

Two people on one workspace: the world's root, and a peer who cloned it, fetched the names and checked out. Pull
fast-forwards or merges as git pull does; every ref is decided before any moves, so a refused pull changes nothing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import griplink
from gr2.python_cli.griplink import GriplinkRefusal
from gr2.python_cli.app import app
from tests.conftest import make_cli_runner

from tests.test_griplink_step1 import _malformed_ls_remote, git, world  # noqa: F401  (api and web, pushed once)


def _peer(w: dict, name: str = "peer") -> Path:
    p = w["tmp"] / name
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{w['remote']}", str(p)], check=True, capture_output=True)
    git(p, "fetch", "-q", "origin", f"+{griplink.NS}/*:{griplink.NS}/*")
    griplink.checkout(p)
    return p


def _write(root: Path, m: str, path: str, text: str) -> None:
    f = root / m / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def _read(root: Path, m: str, path: str) -> str | None:
    f = griplink.member_dir(root, m) / path
    return f.read_text() if f.exists() else None


def _state(root: Path) -> tuple:
    refs = git(root, "for-each-ref", "--format=%(refname) %(objectname)", griplink.NS, "refs/heads")
    return refs, git(root, "rev-parse", "HEAD"), (root / ".gitmodules").read_bytes()


def test_a_pull_with_nothing_new_is_up_to_date(world):
    assert griplink.pull(world["ws"])["how"] == "up to date"


def test_a_pull_fast_forwards_onto_a_peers_push_and_checks_the_change_out(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "api", "src/f1.py", "from the peer\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    got = griplink.pull(ws)
    assert got["how"] == "fast-forward", got
    assert git(ws, "rev-parse", "HEAD") == git(world["remote"], "rev-parse", "main")
    assert git(ws, "rev-parse", griplink.STOOK) == git(world["remote"], "rev-parse", griplink.STOOK)
    assert _read(ws, "api", "src/f1.py") == "from the peer\n"


def test_two_sides_on_different_members_merge_and_the_merge_pushes(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "web", "src/f3.py", "peer on web\n")
    griplink.commit(p, "peer web", stage_all=True)
    griplink.push(p)
    _write(ws, "api", "src/f1.py", "ws on api\n")
    griplink.commit(ws, "ws api", stage_all=True)
    mine = git(ws, "rev-parse", "HEAD")
    got = griplink.pull(ws)
    assert got["how"] == "merged" and got["merged"] == [], got
    parents = git(ws, "rev-list", "--parents", "-n1", "HEAD").split()[1:]
    assert parents[:2] == [mine, git(world["remote"], "rev-parse", "main")], parents
    assert _read(ws, "api", "src/f1.py") == "ws on api\n" and _read(ws, "web", "src/f3.py") == "peer on web\n"
    griplink.push(ws)  # the remote shadow is now an ancestor of ours
    assert git(world["remote"], "rev-parse", "main") == git(ws, "rev-parse", "HEAD")


def test_both_sides_changing_one_member_on_different_files_merge_its_chain(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "api", "src/f3.py", "peer f3\n")
    griplink.commit(p, "peer api", stage_all=True)
    griplink.push(p)
    _write(ws, "api", "src/f1.py", "ws f1\n")
    griplink.commit(ws, "ws api", stage_all=True)
    mine = git(ws, "rev-parse", griplink.member_ref("api"))
    got = griplink.pull(ws)
    assert got["merged"] == ["api"], got
    tip = git(ws, "rev-parse", griplink.member_ref("api"))
    assert git(ws, "rev-list", "--parents", "-n1", tip).split()[1:] == [
        mine, git(world["remote"], "rev-parse", griplink.member_ref("api"))]
    assert _read(ws, "api", "src/f1.py") == "ws f1\n" and _read(ws, "api", "src/f3.py") == "peer f3\n"


def test_a_real_conflict_in_one_member_refuses_by_member_and_path_and_changes_nothing(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "api", "src/f1.py", "peer says this\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    _write(ws, "api", "src/f1.py", "ws says that\n")
    griplink.commit(ws, "ws", stage_all=True)
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: a real conflict in 'src/f1.py'"), str(exc.value)
    assert _state(ws) == before
    assert _read(ws, "api", "src/f1.py") == "ws says that\n"


def test_work_not_in_a_commit_refuses_the_pull_and_changes_nothing(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "web", "src/f3.py", "peer\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    _write(ws, "api", "src/f2.py", "not committed\n")
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("your changes in api are not committed"), str(exc.value)
    assert _state(ws) == before and _read(ws, "api", "src/f2.py") == "not committed\n"


def test_a_record_moved_on_both_sides_refuses(world):
    ws = world["ws"]
    griplink.record(ws, "api", "r1", text="first")
    griplink.commit(ws, "r1")
    griplink.push(ws)
    p = _peer(world)
    griplink.record(p, "api", "r1", text="peer's")
    griplink.commit(p, "peer r1")
    griplink.push(p)
    griplink.record(ws, "api", "r1", text="ours")
    griplink.commit(ws, "ws r1")
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("record r1: both sides moved it"), str(exc.value)
    assert _state(ws) == before


def _member_commit(w: dict, m: str, text: str, parent: str | None = None) -> str:
    """A new commit on member m's own remote (as someone promoting into the member would make), returned."""
    src = w["tmp"] / f"src-{m}"
    if parent:
        git(src, "checkout", "-q", "--detach", parent)
    (src / "src" / "pinned.txt").write_text(text)
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", text)
    sha = git(src, "rev-parse", "HEAD")
    git(src, "push", "-q", "-f", str(w["tmp"] / f"remote-{m}.git"), f"{sha}:refs/heads/x-{sha[:7]}")
    return sha


def _move_pin(root: Path, m: str, sha: str) -> None:
    """A pin move at the root (a promote, or a plain git commit of the gitlink), cut as a shadow."""
    git(griplink.member_dir(root, m), "fetch", "-q", "origin", sha)
    git(root, "update-index", "--cacheinfo", f"160000,{sha},{m}.griplink/{griplink.PIN}")
    griplink._cut(root, f"pin {m}", [git(root, "rev-parse", "HEAD")])
    griplink.checkout(root)  # the member's checkout follows its pin, as after a promote


def test_a_pin_both_sides_moved_takes_the_one_that_contains_the_other(world):
    ws = world["ws"]
    p = _peer(world)
    base = git(griplink.member_dir(ws, "api"), "rev-parse", "HEAD")
    one = _member_commit(world, "api", "one", base)
    two = _member_commit(world, "api", "two", one)
    _move_pin(p, "api", two)
    griplink.push(p)
    _move_pin(ws, "api", one)
    _write(ws, "web", "src/f1.py", "so the shadows diverge\n")
    griplink.commit(ws, "ws", stage_all=True)
    assert griplink.pull(ws)["how"] == "merged"
    assert git(ws, "rev-parse", f"HEAD:api.griplink/{griplink.PIN}") == two


def test_a_pin_both_sides_moved_apart_refuses_by_member(world):
    ws = world["ws"]
    p = _peer(world)
    base = git(griplink.member_dir(ws, "api"), "rev-parse", "HEAD")
    theirs = _member_commit(world, "api", "theirs", base)
    ours = _member_commit(world, "api", "ours", base)
    _move_pin(p, "api", theirs)
    griplink.push(p)
    _move_pin(ws, "api", ours)
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith(
        "api: both sides moved its pin and neither contains the other"), str(exc.value)
    assert _state(ws) == before


def _bad_url_pushed(w: dict) -> None:
    """A peer commits a .gitmodules url gr2 will not pass to git, with plain git, and pushes it."""
    p = _peer(w, "bad")
    git(p, "config", "-f", ".gitmodules", "submodule.api.url", "-uploadpack=x")
    git(p, "add", ".gitmodules")
    git(p, "commit", "-q", "-m", "a url gr2 refuses")
    git(p, "push", "-q", "origin", "main")


def test_a_remote_shadow_the_checkout_would_refuse_is_refused_before_a_fast_forward_moves_anything(world):
    ws = world["ws"]
    _bad_url_pushed(world)
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: the url '-uploadpack=x'"), str(exc.value)
    assert _state(ws) == before


def test_a_remote_shadow_the_checkout_would_refuse_is_refused_before_a_merge_moves_anything(world):
    ws = world["ws"]
    _bad_url_pushed(world)
    _write(ws, "web", "src/f1.py", "so the shadows diverge\n")
    griplink.commit(ws, "ws", stage_all=True)
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: the url '-uploadpack=x'"), str(exc.value)
    assert _state(ws) == before
    assert not (ws / ".git" / "MERGE_HEAD").exists()


def test_a_name_both_sides_set_differently_refuses_by_field(world):
    ws = world["ws"]
    _write(ws, "api", "src/f2.py", "a first tip on api's branch\n")
    griplink.commit(ws, "ws api", stage_all=True)
    griplink.push(ws)
    p = _peer(world)
    _write(p, "api", "src/f3.py", "peer moves api's chain\n")
    griplink.commit(p, "peer api", stage_all=True)
    griplink.pin(p, "api")  # the peer's head: the new tip
    griplink.commit(p, "peer pins api")
    griplink.push(p)
    griplink.pin(ws, "api")  # ours: the tip before the peer's change
    griplink.commit(ws, "ws pins api")
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    said = str(exc.value)
    assert exc.value.code == 3 and said.startswith("api: both sides changed its name (ours pinned "), said
    assert ", theirs pinned " in said and said.endswith("next: pin or follow it, then pull again"), said
    assert _state(ws) == before
    assert not (ws / ".git" / "MERGE_HEAD").exists()


def test_a_name_both_sides_set_to_the_same_value_merges(world):
    ws = world["ws"]
    _write(ws, "api", "src/f2.py", "a first tip on api's branch\n")
    griplink.commit(ws, "ws api", stage_all=True)
    griplink.push(ws)
    p = _peer(world)
    griplink.pin(p, "api")
    griplink.commit(p, "peer pins api")
    griplink.push(p)
    griplink.pin(ws, "api")  # the same tip as the peer's
    griplink.commit(ws, "ws pins api")
    assert griplink.pull(ws)["how"] == "merged"
    tip = git(ws, "rev-parse", griplink.member_ref("api"))
    assert git(ws, "cat-file", "blob", f"{griplink.STOOK}:api.griplink/{griplink.HEADF}").strip() == tip


# ---- a member with no checkout: its pin is read before any ref moves ---------------------------------------------
def _desk(w: dict, name: str = "desk") -> Path:
    """A clone of the root with the root's names, never checked out: no member has a checkout here."""
    d = w["tmp"] / name
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{w['remote']}", str(d)], check=True, capture_output=True)
    git(d, "fetch", "-q", "origin", f"+{griplink.NS}/*:{griplink.NS}/*")
    return d


def _scratches(root: Path) -> list[str]:
    st = griplink._git_dir(root) / "gr2" / "pull-scratch"
    return sorted(p.name for p in st.iterdir()) if st.exists() else []


def _desk_state(root: Path) -> tuple:
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.relative_to(root).parts)
    return _state(root), git(root, "status", "--porcelain"), files


def _pin_on_no_remote(w: dict, m: str = "api") -> str:
    """A peer commits in member m's own git and moves m's pin to it, as promote does; the root push sends no member,
    so the new pin is on no remote. Returns it."""
    p = _peer(w, "promoter")
    mrepo = griplink.member_dir(p, m)
    (mrepo / "src" / "f1.py").write_text("promoted, never pushed to the member's remote\n")
    git(mrepo, "commit", "-q", "-am", "promoted")
    sha = git(mrepo, "rev-parse", "HEAD")
    git(p, "update-index", "--cacheinfo", f"160000,{sha},{m}.griplink/{griplink.PIN}")
    griplink._cut(p, f"pin {m}", [git(p, "rev-parse", "HEAD")])
    griplink.push(p)
    return sha


def _theirs_said(m: str, sha: str) -> str:
    """The refusal for a pin someone else pushed to the root and m's remote does not serve."""
    return (f"{m}: its remote does not serve {sha[:12]}; nothing was pulled. next: whoever pushed this root's pin for "
            f"{m} publishes {sha[:12]} to {m}'s remote, then gr2 pull again")


def _mine_said(m: str, sha: str) -> str:
    """The refusal for a pin made on this desk that m's remote does not serve."""
    return (f"{m}: its remote does not serve {sha[:12]}; nothing was pulled. next: this root's pin {sha[:12]} was made "
            f"on this desk and is not on {m}'s remote; push it to {m}'s remote from where it was made (or move the pin), "
            "then gr2 pull again")


def _pin_served(w: dict, m: str = "api") -> str:
    """A peer moves member m's pin to a commit m's own remote serves, and pushes the root. Returns the pin."""
    p = _peer(w, "mover")
    sha = _member_commit(w, m, "served")
    _move_pin(p, m, sha)
    griplink.push(p)
    return sha


def _diverge_without_api(w: dict, d: Path) -> None:
    """Desk d checks out, changes web and commits (so a pull merges), then loses api's checkout."""
    griplink.checkout(d)
    _write(d, "web", "src/f1.py", "the desk's own change\n")
    griplink.commit(d, "desk web", stage_all=True)
    import shutil
    shutil.rmtree(griplink.member_dir(d, "api"))


def _add_member(w: dict, p: Path, m: str = "lib", serve: bool = True) -> str:
    """Peer p adds member m from a new remote; with serve=False its pin then moves to a commit only p holds."""
    git(w["tmp"], "init", "-q", "--bare", "-b", "main", str(w["tmp"] / f"remote-{m}.git"))
    src = w["tmp"] / f"src-{m}"
    (src / "src").mkdir(parents=True)
    git(src, "init", "-q", "-b", "main")
    (src / "src" / "l.py").write_text(f"{m} v1\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", f"{m}: v1")
    git(src, "push", "-q", str(w["tmp"] / f"remote-{m}.git"), "HEAD:main")
    griplink.add(p, m, str(w["tmp"] / f"remote-{m}.git"))
    if not serve:
        mrepo = griplink.member_dir(p, m)
        (mrepo / "src" / "l.py").write_text("only the peer holds this\n")
        git(mrepo, "commit", "-q", "-am", "unserved")
        git(p, "update-index", "--cacheinfo", f"160000,{git(mrepo, 'rev-parse', 'HEAD')},{m}.griplink/{griplink.PIN}")
        griplink._cut(p, f"pin {m}", [git(p, "rev-parse", "HEAD")])
    griplink.push(p)
    return git(p, "rev-parse", f"HEAD:{m}.griplink/{griplink.PIN}")


def test_a_pin_on_no_remote_refuses_a_fast_forward_on_a_desk_with_no_checkout_and_moves_nothing(world):
    d = _desk(world)
    pin = _pin_on_no_remote(world)
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", pin)
    assert _desk_state(d) == before and _scratches(d) == []


def test_a_pin_on_no_remote_refuses_a_merge_on_a_desk_with_no_api_checkout_and_moves_nothing(world):
    d = _desk(world)
    _diverge_without_api(world, d)
    pin = _pin_on_no_remote(world)
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", pin)
    assert _desk_state(d) == before and _scratches(d) == []
    assert not (d / ".git" / "MERGE_HEAD").exists()


def test_a_served_pin_fast_forwards_a_desk_with_no_checkout_and_checks_every_member_out(world, monkeypatch):
    d = _desk(world)
    pin = _pin_served(world)
    clones = []
    run = griplink.gitops.run

    def counted(repo, *args, **kw):
        if args[:1] == ("clone",):
            clones.append(args[-1])
        return run(repo, *args, **kw)

    monkeypatch.setattr(griplink.gitops, "run", counted)
    got = griplink.pull(d)
    assert len(clones) == 2, clones  # one clone per member: checkout moves the scratch into place, never clones again
    assert got["how"] == "fast-forward", got
    assert git(griplink.member_dir(d, "api"), "rev-parse", "HEAD") == pin
    assert (griplink.member_dir(d, "web") / ".git").exists()
    assert _scratches(d) == []
    assert git(griplink.member_dir(d, "api"), "config", "--get", "remote.origin.url") == str(
        world["tmp"] / "remote-api.git")


def test_a_served_pin_merges_on_a_desk_with_no_api_checkout(world):
    d = _desk(world)
    _diverge_without_api(world, d)
    pin = _pin_served(world)
    assert griplink.pull(d)["how"] == "merged"
    assert git(griplink.member_dir(d, "api"), "rev-parse", "HEAD") == pin
    assert _read(d, "web", "src/f1.py") == "the desk's own change\n"
    assert _scratches(d) == []


@pytest.mark.parametrize("diverge", [False, True], ids=["fast-forward", "merge"])
def test_a_pull_that_adds_a_member_its_remote_serves_checks_it_out(world, diverge):
    ws = world["ws"]
    if diverge:
        _write(ws, "web", "src/f1.py", "so the shadows diverge\n")
        griplink.commit(ws, "ws", stage_all=True)
    pin = _add_member(world, _peer(world))
    assert griplink.pull(ws)["how"] == ("merged" if diverge else "fast-forward")
    assert git(griplink.member_dir(ws, "lib"), "rev-parse", "HEAD") == pin
    assert _read(ws, "lib", "src/l.py") == "lib v1\n" and _scratches(ws) == []


@pytest.mark.parametrize("diverge", [False, True], ids=["fast-forward", "merge"])
def test_a_pull_that_adds_a_member_whose_pin_is_not_served_refuses_and_moves_nothing(world, diverge):
    ws = world["ws"]
    if diverge:
        _write(ws, "web", "src/f1.py", "so the shadows diverge\n")
        griplink.commit(ws, "ws", stage_all=True)
    pin = _add_member(world, _peer(world), serve=False)
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("lib", pin)
    assert _desk_state(ws) == before and _scratches(ws) == []
    assert not (ws / "lib.griplink").exists()


@pytest.mark.parametrize("diverge", [False, True], ids=["fast-forward", "merge"])
def test_a_later_refusal_removes_the_scratch_it_made(world, diverge):
    ws = world["ws"]
    if diverge:
        _write(ws, "web", "src/f1.py", "so the shadows diverge\n")
        griplink.commit(ws, "ws", stage_all=True)
    p = _peer(world, "adder")
    _add_member(world, p)
    git(p, "config", "-f", ".gitmodules", "submodule.api.url", "-uploadpack=x")
    git(p, "add", ".gitmodules")
    git(p, "commit", "-q", "-m", "a url gr2 refuses")
    git(p, "push", "-q", "origin", "main")
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 3 and str(exc.value).startswith("api: the url '-uploadpack=x'"), str(exc.value)
    assert _desk_state(ws) == before and _scratches(ws) == []


def test_a_scratch_left_by_a_killed_pull_is_never_read_as_this_pulls_fetch(world):
    """A pull killed between its scratch and its ref writes leaves the scratch. Here the stale scratch holds the very pin,
    which the remote no longer serves; the next pull must not take it as fetched."""
    d = _desk(world)
    pin = _pin_on_no_remote(world)
    st = griplink._git_dir(d) / "gr2" / "pull-scratch" / "api"
    st.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(world["tmp"] / "remote-api.git"), str(st)], check=True,
                   capture_output=True)
    git(st, "fetch", "-q", str(griplink.member_dir(world["tmp"] / "promoter", "api")), pin)
    assert griplink._has(st, pin)  # the precondition: the stale scratch holds the pin
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", pin)
    assert _desk_state(d) == before and _scratches(d) == []


def test_checkout_takes_a_scratch_only_when_it_holds_the_pin_from_the_members_url(world):
    d = _desk(world)
    st = griplink._git_dir(d) / "gr2" / "pull-scratch" / "api"
    st.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(world["tmp"] / "remote-web.git"), str(st)], check=True,
                   capture_output=True)  # a scratch from the wrong url
    pin = git(d, "rev-parse", f"HEAD:api.griplink/{griplink.PIN}")
    git(st, "fetch", "-q", str(world["tmp"] / "remote-api.git"), pin)
    assert griplink._has(st, pin)  # the precondition: only its url tells it apart
    griplink.checkout(d)
    api = griplink.member_dir(d, "api")
    assert git(api, "config", "--get", "remote.origin.url") == str(world["tmp"] / "remote-api.git")
    assert git(api, "rev-parse", "HEAD") == git(d, "rev-parse", f"HEAD:api.griplink/{griplink.PIN}")
    assert _scratches(d) == []


def test_a_scratch_that_cannot_be_removed_is_named_in_the_refusal(world, monkeypatch):
    d = _desk(world)
    pin = _pin_on_no_remote(world)
    real = griplink.rmtree_or_refuse

    def stuck(p: Path) -> None:
        if p.exists():
            raise griplink.IncompleteRemoval(f"{p} could not be fully removed")
        real(p)

    monkeypatch.setattr(griplink, "rmtree_or_refuse", stuck)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    said = str(exc.value)
    assert exc.value.code == 5 and said.startswith(_theirs_said("api", pin)), said
    assert "; and a scratch could not be removed: " in said and "pull-scratch/api could not be fully removed" in said, said


# ---- the merge path reads the pins the merge records, mine where only my side moved one ----------------------------
def _local_pin(d: Path, m: str = "api") -> str:
    """Desk d moves member m's pin to a commit only d holds (a promote whose member was never pushed)."""
    mrepo = griplink.member_dir(d, m)
    (mrepo / "src" / "f2.py").write_text("the desk's own promote, never pushed\n")
    git(mrepo, "add", "src/f2.py")
    git(mrepo, "commit", "-q", "-m", "desk promote")
    sha = git(mrepo, "rev-parse", "HEAD")
    git(d, "update-index", "--cacheinfo", f"160000,{sha},{m}.griplink/{griplink.PIN}")
    griplink._cut(d, f"pin {m}", [git(d, "rev-parse", "HEAD")])
    return sha


def _mine_moved(w: dict, push: bool = False, keep_checkout: bool = False) -> tuple[Path, str]:
    """A desk moves api's pin to its own commit (pushed to api's remote or not), keeps or loses api's checkout, and a
    peer moves web, so the pull merges with api's merged pin being MINE."""
    d = _desk(w)
    griplink.checkout(d)
    mine = _local_pin(d)
    if push:
        git(griplink.member_dir(d, "api"), "push", "-q", str(w["tmp"] / "remote-api.git"), f"{mine}:refs/heads/x-mine")
    if not keep_checkout:
        import shutil
        shutil.rmtree(griplink.member_dir(d, "api"))
    _pin_served(w, "web")
    return d, mine


def test_a_merge_whose_pin_is_mine_and_on_no_remote_refuses_with_no_checkout_and_moves_nothing(world):
    d, mine = _mine_moved(world)
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 5 and str(exc.value) == _mine_said("api", mine)
    assert _desk_state(d) == before and _scratches(d) == []
    assert not (d / ".git" / "MERGE_HEAD").exists()


def test_a_merge_whose_pin_is_mine_and_served_checks_api_out_at_mine(world):
    d, mine = _mine_moved(world, push=True)
    assert griplink.pull(d)["how"] == "merged"
    assert git(d, "rev-parse", f"HEAD:api.griplink/{griplink.PIN}") == mine
    assert git(griplink.member_dir(d, "api"), "rev-parse", "HEAD") == mine and _scratches(d) == []


def test_a_merge_whose_pin_is_mine_and_only_in_my_checkout_still_merges(world):
    """The common path: an unpushed local commit at a member's pin, its checkout present. Readable means my own
    checkout holds it, not only that a remote serves it."""
    d, mine = _mine_moved(world, keep_checkout=True)
    assert griplink.pull(d)["how"] == "merged"
    assert git(d, "rev-parse", f"HEAD:api.griplink/{griplink.PIN}") == mine
    assert git(griplink.member_dir(d, "api"), "rev-parse", "HEAD") == mine


def test_checkout_of_a_pin_no_clone_can_read_refuses_naming_the_member(world):
    d, mine = _mine_moved(world)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(d)
    said = str(exc.value)
    assert exc.value.code == 3 and said.startswith(f"api: cannot check out its pin {mine[:12]}: "), said
    assert said.endswith("then gr2 checkout again"), said


def test_checkout_of_a_member_its_url_cannot_clone_refuses_naming_the_member(world):
    d = _desk(world)
    url = str(world["tmp"] / "remote-api.git")
    (world["tmp"] / "remote-api.git").rename(world["tmp"] / "remote-api.gone")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.checkout(d)
    said = str(exc.value)
    head, tail = f"api: cannot clone it from {url}: ", "; next: check the url in .gitmodules, then gr2 checkout again"
    assert exc.value.code == 3 and said.startswith(head) and said.endswith(tail), said
    # git's own reason sits between them; its wording varies by git version, so only its presence is asserted
    assert said[len(head):-len(tail)].strip(), said
    assert not (griplink.member_dir(d, "api") / ".git").exists() and _scratches(d) == []


def test_a_merge_reads_a_member_only_my_side_added(world):
    d = _desk(world)
    griplink.checkout(d)
    tmp = world["tmp"]
    git(tmp, "init", "-q", "--bare", "-b", "main", str(tmp / "remote-zed.git"))
    src = tmp / "src-zed"
    (src / "src").mkdir(parents=True)
    git(src, "init", "-q", "-b", "main")
    (src / "src" / "z.py").write_text("zed v1\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "zed: v1")
    git(src, "push", "-q", str(tmp / "remote-zed.git"), "HEAD:main")
    griplink.add(d, "zed", str(tmp / "remote-zed.git"))
    mine = _local_pin(d, "zed")
    import shutil
    shutil.rmtree(griplink.member_dir(d, "zed"))
    _pin_served(world, "web")
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 5 and str(exc.value) == _mine_said("zed", mine)
    assert _desk_state(d) == before and _scratches(d) == []


# ---- a refusal for an unserved pin names the next step by who owns the pin -------------------------------------------


def test_a_pin_on_no_remote_refuses_a_fast_forward_on_a_desk_with_its_checkout_naming_whoever_pushed_it(world):
    ws = world["ws"]
    pin = _pin_on_no_remote(world)
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", pin)
    assert _desk_state(ws) == before


def test_a_pin_both_sides_moved_whose_theirs_is_on_no_remote_refuses_naming_whoever_pushed_it(world):
    ws = world["ws"]
    base = git(griplink.member_dir(ws, "api"), "rev-parse", "HEAD")
    one = _member_commit(world, "api", "one", base)
    pin = _pin_on_no_remote(world)
    _move_pin(ws, "api", one)
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", pin)
    assert _desk_state(ws) == before


def test_a_decided_pin_that_is_theirs_and_on_no_remote_refuses_naming_whoever_pushed_it(world, monkeypatch):
    """A decided pin only their side recorded is theirs, so its step is theirs. A pull cannot reach this with a checkout
    (deciding reads both pins in it first), so the decision is stubbed and the checkout removed: the scratch path reads
    the pin."""
    ws = world["ws"]
    pin = _pin_on_no_remote(world)
    ours = _local_pin(ws)
    import shutil
    shutil.rmtree(griplink.member_dir(ws, "api"))
    monkeypatch.setattr(griplink, "_decide_pins", lambda root, base, mine, theirs: {"api": pin})
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert ours != pin
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", pin)
    assert _desk_state(ws) == before and _scratches(ws) == []


def test_a_decided_pin_that_is_my_sha_and_on_no_remote_refuses_naming_this_desk(world, monkeypatch):
    """A decided pin only my side recorded was made on this desk, whatever shadow names its url, so its step is mine.
    A pull cannot reach this with a checkout (deciding reads both pins in it first), so the decision is stubbed and the
    checkout removed: the scratch path reads the pin."""
    ws = world["ws"]
    _pin_served(world, "api")
    ours = _local_pin(ws)
    import shutil
    shutil.rmtree(griplink.member_dir(ws, "api"))
    monkeypatch.setattr(griplink, "_decide_pins", lambda root, base, mine, theirs: {"api": ours})
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 5 and str(exc.value) == _mine_said("api", ours)
    assert _desk_state(ws) == before and _scratches(ws) == []


def test_a_pin_both_sides_recorded_and_on_no_remote_refuses_naming_whoever_pushed_it(world):
    """Both sides' root commits record the same api sha, on no remote, and this desk has no api checkout: the pin is
    not only this desk's, so its step is theirs."""
    ws = world["ws"]
    same = _local_pin(ws)
    p = _peer(world, "twin")
    _move_pin(p, "web", _member_commit(world, "web", "twin"))  # first, so no root commit of theirs equals this desk's
    git(griplink.member_dir(p, "api"), "fetch", "-q", str(griplink.member_dir(ws, "api")), same)
    git(p, "update-index", "--cacheinfo", f"160000,{same},api.griplink/{griplink.PIN}")
    griplink._cut(p, "pin api", [git(p, "rev-parse", "HEAD")])
    griplink.push(p)
    import shutil
    shutil.rmtree(griplink.member_dir(ws, "api"))
    before = _desk_state(ws)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 5 and str(exc.value) == _theirs_said("api", same)
    assert _desk_state(ws) == before and _scratches(ws) == []
    assert not (ws / ".git" / "MERGE_HEAD").exists()


def _pin_lost(w: dict, m: str = "api") -> tuple[Path, str]:
    """A desk moves m's pin to its own commit, then m's checkout loses it (reset to the old pin, reflog expired,
    pruned): m is checked out but does not hold its recorded pin. Returns the desk and the pin."""
    d = _desk(w)
    griplink.checkout(d)
    mine = _local_pin(d, m)
    mrepo = griplink.member_dir(d, m)
    git(mrepo, "reset", "-q", "--hard", f"{mine}^")
    git(mrepo, "update-ref", "-d", "ORIG_HEAD")
    git(mrepo, "reflog", "expire", "--expire=now", "--all")
    git(mrepo, "gc", "-q", "--prune=now")
    assert subprocess.run(["git", "-C", str(mrepo), "cat-file", "-e", f"{mine}^{{commit}}"]).returncode != 0
    return d, mine


def _lost_said(sha: str, done: str, verb: str, m: str = "api") -> str:
    return (f"{m}: its checkout does not hold its recorded pin {sha[:12]}; nothing was {done}. next: fetch {sha[:12]} "
            f"into {m} from where it was made, or move the pin, then gr2 {verb} again")


def test_a_checkout_that_lost_its_recorded_pin_refuses_the_pull_and_changes_nothing(world):
    d, mine = _pin_lost(world)
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "pulled", "pull")
    assert _desk_state(d) == before and _scratches(d) == []


def test_a_checkout_that_lost_its_pin_refuses_the_pull_even_when_a_peer_moved_api_to_a_served_pin(world):
    d, mine = _pin_lost(world)
    _pin_served(world)
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "pulled", "pull")
    assert _desk_state(d) == before and _scratches(d) == []


def test_a_checkout_that_lost_its_recorded_pin_refuses_the_commit_and_cuts_nothing(world):
    d, mine = _pin_lost(world)
    _write(d, "api", "src/f3.py", "staged on a lost pin\n")
    git(griplink.member_dir(d, "api"), "add", "src/f3.py")
    before = _state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(d, "lost", stage_all=True)
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "committed", "commit")
    assert _state(d) == before


def test_a_checkout_that_lost_its_recorded_pin_refuses_a_commit_of_what_is_staged_and_cuts_nothing(world):
    d, mine = _pin_lost(world)
    _write(d, "api", "src/f3.py", "staged on a lost pin\n")
    git(griplink.member_dir(d, "api"), "add", "src/f3.py")
    before = _state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(d, "lost")
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "committed", "commit")
    assert _state(d) == before


def test_a_lost_pin_in_a_later_member_is_refused_by_name_when_an_earlier_member_holds_its_own(world):
    d, mine = _pin_lost(world, "web")
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "pulled", "pull", "web")
    assert _desk_state(d) == before and _scratches(d) == []
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(d, "lost", stage_all=True)
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "committed", "commit", "web")


def test_a_commit_of_all_on_a_lost_pin_refuses_before_it_stages_anything(world):
    d, mine = _pin_lost(world)
    api = griplink.member_dir(d, "api")
    (api / "src" / "f1.py").write_text("an unstaged edit on a lost pin\n")
    staged = git(api, "diff", "--cached", "--name-only")
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(d, "lost", stage_all=True)
    assert exc.value.code == 3 and str(exc.value) == _lost_said(mine, "committed", "commit")
    assert git(api, "diff", "--cached", "--name-only") == staged


def _no_api_checkout(w: dict) -> Path:
    d = _desk(w)
    griplink.checkout(d)
    import shutil
    shutil.rmtree(griplink.member_dir(d, "api"))
    return d


def test_a_member_with_no_checkout_beside_one_holding_its_pin_is_not_refused_by_pull(world):
    d = _no_api_checkout(world)
    griplink.pull(d)


def test_a_commit_beside_a_member_with_no_checkout_cuts_the_staged_change(world):
    d = _no_api_checkout(world)
    before = _local_ref(d, griplink.member_ref("api"))
    _write(d, "web", "src/f3.py", "the desk's own change\n")
    git(griplink.member_dir(d, "web"), "add", "src/f3.py")
    done = griplink.commit(d, "desk web")
    names = git(d, "ls-tree", "-r", "--name-only", griplink.member_ref("web")).splitlines()
    assert any(n.endswith("overlay/src/f3.py") for n in names)
    assert done["skipped"] == ["api"] and done["moved"] == ["web"]
    assert _local_ref(d, griplink.member_ref("api")) == before


def _local_ref(d: Path, ref: str) -> str | None:
    done = subprocess.run(["git", "-C", str(d), "rev-parse", "-q", "--verify", ref], capture_output=True, text=True)
    return done.stdout.strip() or None


def test_a_commit_all_beside_a_member_with_no_checkout_stages_and_cuts_the_tracked_edit(world):
    d = _no_api_checkout(world)
    web = griplink.member_dir(d, "web")
    tracked = git(web, "ls-files").splitlines()[0]
    (web / tracked).write_text("an unstaged edit\n")
    done = griplink.commit(d, "desk web all", stage_all=True)
    names = git(d, "ls-tree", "-r", "--name-only", griplink.member_ref("web")).splitlines()
    assert any(n.endswith(f"overlay/{tracked}") for n in names)
    assert done["skipped"] == ["api"]


def test_a_commit_beside_a_member_with_no_checkout_and_nothing_staged_refuses_with_nothing_to_commit(world):
    d = _no_api_checkout(world)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.commit(d, "nothing")
    assert exc.value.code == 1 and str(exc.value) == "nothing to commit, working tree clean"


def test_the_cli_commit_beside_a_member_with_no_checkout_says_it_skipped_the_member(world, monkeypatch):
    from gr2.python_cli.app import app
    from tests.conftest import make_cli_runner

    d = _no_api_checkout(world)
    _write(d, "web", "src/f3.py", "the desk's own change\n")
    git(griplink.member_dir(d, "web"), "add", "src/f3.py")
    monkeypatch.chdir(d)
    r = make_cli_runner().invoke(app, ["commit", "-m", "desk web"])
    assert r.exit_code == 0, r.output
    assert r.output.count("api: no checkout here; skipped") == 1


# ---- a refusal names only the pins still unserved after the fetch -----------------------------------------------------


def test_member_has_fetches_a_served_pin_beside_an_unserved_one_and_names_only_the_unserved(world):
    """git fails a whole fetch when one sha in it is not served, so a served sha asked for beside an unserved one used to
    stay missing and be named unserved. Only the pin no remote serves is named, and the served one is fetched."""
    ws = world["ws"]
    served = _member_commit(world, "api", "served beside an unserved one")
    src = world["tmp"] / "src-api"
    (src / "src" / "pinned.txt").write_text("made here, never pushed\n")
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "never pushed")
    unserved = git(src, "rev-parse", "HEAD")
    mrepo = griplink.member_dir(ws, "api")
    for s in (served, unserved):
        assert subprocess.run(["git", "-C", str(mrepo), "cat-file", "-e", f"{s}^{{commit}}"]).returncode != 0
    with pytest.raises(GriplinkRefusal) as exc:
        griplink._member_has(ws, "api", unserved, served, mine=(unserved,))
    assert exc.value.code == 5 and str(exc.value) == _mine_said("api", unserved)
    assert subprocess.run(["git", "-C", str(mrepo), "cat-file", "-e", f"{served}^{{commit}}"]).returncode == 0


def test_a_pull_whose_head_pin_is_lost_and_whose_peer_pin_is_served_names_only_the_lost_pin(world):
    """This desk's HEAD records api at a commit its checkout no longer holds, its index stages a pin the checkout does
    hold (so the pin-held check passes), and a peer moved api to a served pin: the pin decision reads both, and the
    refusal names only this desk's pin, not the peer's served one."""
    d, mine = _pin_lost(world)
    held = git(griplink.member_dir(d, "api"), "rev-parse", "HEAD")
    git(d, "update-index", "--cacheinfo", f"160000,{held},api.griplink/{griplink.PIN}")
    _pin_served(world)
    before = _desk_state(d)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(d)
    assert exc.value.code == 5 and str(exc.value) == _mine_said("api", mine)
    assert _desk_state(d) == before and _scratches(d) == []


def test_a_moved_remote_shadow_with_no_main_refuses_and_moves_nothing(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "api", "src/f1.py", "from the peer\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    git(world["remote"], "update-ref", "-d", "refs/heads/main")
    theirs, mine = git(world["remote"], "rev-parse", griplink.SHADOW), git(ws, "rev-parse", "HEAD")
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.pull(ws)
    assert e.value.code == 5
    assert str(e.value) == (f"origin has no main, but its shadow {theirs[:12]} could not be compared: its commit is not "
                            f"on this desk (this desk is at {mine[:12]}); nothing was pulled. next: ask whoever pushed "
                            f"{theirs[:12]} to push main with it, then pull again")
    assert _state(ws) == before


def test_an_empty_remote_is_up_to_date(world):
    ws = world["ws"]
    for ref in ("refs/heads/main", griplink.SHADOW):
        git(world["remote"], "update-ref", "-d", ref)
    got = griplink.pull(ws)
    assert got == {"how": "up to date", "shadow": git(ws, "rev-parse", "HEAD"), "merged": [], "updated": []}


def test_a_desk_ahead_of_a_remote_shadow_with_no_main_is_up_to_date_and_says_so(world):
    ws = world["ws"]
    theirs = git(world["remote"], "rev-parse", griplink.SHADOW)
    _write(ws, "api", "src/f1.py", "ws ahead\n")
    griplink.commit(ws, "ws ahead", stage_all=True)
    git(world["remote"], "update-ref", "-d", "refs/heads/main")
    before = _state(ws)
    got = griplink.pull(ws)
    assert got["how"] == "up to date" and got["shadow"] == git(ws, "rev-parse", "HEAD") != theirs
    assert got["note"] == (f"origin has no main; its shadow {theirs[:12]} is already in this desk's history. next: gr2 "
                           "push from this desk to give it a main")
    assert _state(ws) == before


def test_a_remote_shadow_here_but_not_in_this_desks_history_refuses_and_moves_nothing(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "api", "src/f1.py", "from the peer\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    git(world["remote"], "update-ref", "-d", "refs/heads/main")
    git(ws, "fetch", "-q", str(world["remote"]), griplink.SHADOW)
    theirs, mine = git(world["remote"], "rev-parse", griplink.SHADOW), git(ws, "rev-parse", "HEAD")
    git(ws, "cat-file", "-e", theirs)
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.pull(ws)
    assert e.value.code == 5
    assert str(e.value) == (f"origin has no main, but its shadow {theirs[:12]} is not in this desk's history (this desk "
                            f"is at {mine[:12]}); nothing was pulled. next: ask whoever pushed {theirs[:12]} to push "
                            "main with it, then pull again")
    assert _state(ws) == before


def test_the_cli_prints_the_no_main_note_once(world, monkeypatch):
    ws = world["ws"]
    theirs = git(world["remote"], "rev-parse", griplink.SHADOW)
    _write(ws, "api", "src/f1.py", "ws ahead\n")
    griplink.commit(ws, "ws ahead", stage_all=True)
    git(world["remote"], "update-ref", "-d", "refs/heads/main")
    monkeypatch.chdir(ws)
    got = make_cli_runner().invoke(app, ["pull"])
    assert got.exit_code == 0, got.output
    note = (f"origin has no main; its shadow {theirs[:12]} is already in this desk's history. next: gr2 push from this "
            "desk to give it a main")
    assert got.output.splitlines().count(note) == 1


def test_a_lookalike_shadow_listed_first_does_not_hide_a_moved_remote_shadow(world):
    ws = world["ws"]
    p = _peer(world)
    _write(p, "api", "src/f1.py", "from the peer\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    git(world["remote"], "update-ref", "-d", "refs/heads/main")
    theirs, mine = git(world["remote"], "rev-parse", griplink.SHADOW), git(ws, "rev-parse", "HEAD")
    git(world["remote"], "update-ref", f"refs/a/{griplink.SHADOW}", mine)
    before = _state(ws)
    with pytest.raises(GriplinkRefusal) as e:
        griplink.pull(ws)
    assert e.value.code == 5
    assert str(e.value) == (f"origin has no main, but its shadow {theirs[:12]} could not be compared: its commit is not "
                            f"on this desk (this desk is at {mine[:12]}); nothing was pulled. next: ask whoever pushed "
                            f"{theirs[:12]} to push main with it, then pull again")
    assert _state(ws) == before


def test_a_remote_with_no_main_and_only_a_lookalike_shadow_is_up_to_date(world):
    ws = world["ws"]
    mine = git(ws, "rev-parse", "HEAD")
    for ref in ("refs/heads/main", griplink.SHADOW):
        git(world["remote"], "update-ref", "-d", ref)
    git(world["remote"], "update-ref", f"refs/a/{griplink.SHADOW}", mine)
    got = griplink.pull(ws)
    assert got == {"how": "up to date", "shadow": mine, "merged": [], "updated": []}


def test_a_remote_with_no_main_and_only_a_lookalike_main_takes_the_no_main_path(world):
    ws = world["ws"]
    mine = git(ws, "rev-parse", "HEAD")
    for ref in ("refs/heads/main", griplink.SHADOW):
        git(world["remote"], "update-ref", "-d", ref)
    git(world["remote"], "update-ref", "refs/a/refs/heads/main", mine)
    before = _state(ws)
    got = griplink.pull(ws)
    assert got == {"how": "up to date", "shadow": mine, "merged": [], "updated": []}
    assert _state(ws) == before


def test_a_lookalike_main_listed_first_does_not_hide_a_moved_real_main(world):
    ws = world["ws"]
    old = git(ws, "rev-parse", "HEAD")
    p = _peer(world)
    _write(p, "api", "src/f1.py", "from the peer\n")
    griplink.commit(p, "peer", stage_all=True)
    griplink.push(p)
    theirs = git(world["remote"], "rev-parse", "refs/heads/main")
    git(world["remote"], "update-ref", "refs/a/refs/heads/main", old)
    rows = git(ws, "ls-remote", str(world["remote"]), "refs/heads/main")
    print(rows)
    assert rows.splitlines() == [f"{old}\trefs/a/refs/heads/main", f"{theirs}\trefs/heads/main"]
    griplink.pull(ws)
    assert git(ws, "rev-parse", "HEAD") == theirs != old
    assert (ws / "api" / "src" / "f1.py").read_text() == "from the peer\n"


def test_pull_names_a_malformed_listing_as_unreadable_not_as_two_mains(world, monkeypatch):
    ws = world["ws"]
    before = _state(ws)
    _malformed_ls_remote(monkeypatch)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert exc.value.code == 5
    assert str(exc.value) == ("cannot read the remote shadow from origin: a row of its listing is malformed; "
                              "nothing was pulled")
    assert _state(ws) == before


def test_pull_from_a_remote_with_no_main_names_a_malformed_shadow_listing_as_unreadable(world, monkeypatch):
    ws = world["ws"]
    git(world["remote"], "update-ref", "-d", "refs/heads/main")
    before = _state(ws)
    _malformed_ls_remote(monkeypatch, griplink.SHADOW)
    real_no_main, calls = griplink._pull_no_main, []

    def counted(root, remote):
        calls.append(remote)
        return real_no_main(root, remote)

    monkeypatch.setattr(griplink, "_pull_no_main", counted)
    with pytest.raises(GriplinkRefusal) as exc:
        griplink.pull(ws)
    assert calls == ["origin"]
    assert exc.value.code == 5
    assert str(exc.value) == ("cannot read the remote shadow from origin: a row of its listing is malformed; "
                              "nothing was pulled")
    assert _state(ws) == before

"""A bare `review pin` at a full root (`store init --mode full`, members added by `gr2 add`: no workspace spec and no
grip.toml) reads its members from the root commit's .gitmodules through the one set reader, and pins each from its
gitlink in HEAD. A workspace that declares no members refuses with words that say so, never "every member's checkout is
at the workspace's pin".

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * the full-root branch is dropped (members read as none)               -> the three-ahead, one-ahead, --members rows
  * the pin is read from the root's index, not HEAD                      -> the staged-gitlink row
  * the old default sentence returns for an empty member list            -> the no-members row
  * a reader refusal is read as "no members"                             -> the malformed-.gitmodules row
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import declared_sets
from gr2.python_cli import events as events_mod
from tests.test_full_root_declared_sets import MEMBERS, full  # noqa: F401
from tests.test_griplink_cli import git, gr2

OLD = "every member's checkout is at the workspace's pin"


def _flat(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def _out(result) -> str:
    return _flat((result.stdout or "") + (result.stderr or ""))


def _checkout(root: Path, member: str) -> Path:
    return root / declared_sets.read(root, "HEAD").members[member]["path"]


def _ahead(root: Path, member: str) -> str:
    checkout = _checkout(root, member)
    git(checkout, "checkout", "-q", "-b", "feat/f1")
    (checkout / f"{member}.txt").write_text(f"{member} f1\n")
    git(checkout, "commit", "-q", "-am", f"{member} f1")
    return git(checkout, "rev-parse", "HEAD")


REVIEWS = "refs/dev.synapt.grip/__reviews__/"


def _reviews(root: Path) -> list[str]:
    return git(root, "for-each-ref", "--format=%(refname)", REVIEWS).split()


def _bound(r, out: str, root: Path, reviews: int | None = 1) -> None:
    """The rows are chosen, printed and bound: the root's own .git is the store, so each bind adds one review ref (`reviews`
    in all) and writes nothing in the work tree (no grip.toml, no .grip/)."""
    assert r.exit_code == 0, out
    assert "does not read a full-mode root yet" not in out, out
    assert reviews is None or len(_reviews(root)) == reviews, _reviews(root)
    assert not (root / "grip.toml").exists() and not (root / ".grip").exists()
    assert git(root, "status", "--porcelain", "--ignored", "-uall", "--ignore-submodules=all") == "", out


def _pin(root: Path, member: str) -> str:
    return git(root, "ls-tree", "HEAD", "--", declared_sets.read(root, "HEAD").members[member]["path"]).split()[2]


def test_full_root_every_member_ahead_is_pinned(full, monkeypatch):
    root = full["root"]
    pins = {m: _pin(root, m) for m in MEMBERS}
    heads = {m: _ahead(root, m) for m in MEMBERS}
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bound(r, out, root)
    for m in MEMBERS:
        assert f"{m} {pins[m][:12]}..{heads[m][:12]}" in out or (pins[m] in out and heads[m] in out), out
    assert "nothing to pin" not in out


def test_full_root_one_member_ahead_names_the_other(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bound(r, out, root)
    assert "web (checkout is at the workspace's pin)" in out, out


def test_full_root_members_flag_narrows(full, monkeypatch):
    root = full["root"]
    for m in MEMBERS:
        _ahead(root, m)
    r = gr2(root, monkeypatch, "review", "pin", "--members", "web")
    out = _out(r)
    _bound(r, out, root)
    assert "api (excluded by --members)" in out, out


def test_full_root_staged_gitlink_is_not_the_base(full, monkeypatch):
    root = full["root"]
    pin = _pin(root, "api")
    head = _ahead(root, "api")
    git(root, "add", declared_sets.read(root, "HEAD").members["api"]["path"])  # staged, not committed
    assert git(root, "ls-files", "-s", "--", declared_sets.read(root, "HEAD").members["api"]["path"]).split()[1] == head
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bound(r, out, root)
    assert pin[:12] in out and head[:12] in out, out


def test_full_root_unreadable_gitmodules_refuses_by_name(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    (root / ".gitmodules").write_text((root / ".gitmodules").read_text() + "[broken\n")
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "broken gitmodules")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == 2, out
    assert "gitmodules_unreadable" in out, out
    assert "nothing to pin" not in out


def test_past_gitmodules_that_does_not_parse_is_skipped_once_fixed(full):
    """A .gitmodules git cannot parse in an old commit, fixed on top: git reads the root (fsck --strict only warns), so
    the history check skips that blob and the read keeps its members."""
    root = full["root"]
    good = (root / ".gitmodules").read_text()
    (root / ".gitmodules").write_text(good + '[submodule "a"\n')
    git(root, "commit", "-q", "-m", "unclosed section", "--", ".gitmodules")
    assert subprocess.run(["git", "config", "--blob", "HEAD:.gitmodules", "--list"], cwd=root,
                          capture_output=True).returncode == 128
    (root / ".gitmodules").write_text(good)
    git(root, "commit", "-q", "-m", "fixed on top", "--", ".gitmodules")
    assert "api" in declared_sets.read(root, "HEAD").members


def test_full_root_after_root_commit_rows_from_head_pins(full, monkeypatch):
    """A root `gr2 commit` (rc 0) does not move the members' gitlinks in this fixture; the rows still go from the pins in
    HEAD to the checkouts, as measured."""
    root = full["root"]
    heads = {m: _ahead(root, m) for m in MEMBERS}
    c = gr2(root, monkeypatch, "commit", "-m", "root f1")
    assert c.exit_code == 0, _out(c)
    pins = {m: _pin(root, m) for m in MEMBERS}
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bound(r, out, root)
    for m in MEMBERS:
        assert f"pin {m} {pins[m][:12]}..{heads[m][:12]}" in out, out
    assert "nothing to pin" not in out


def test_full_root_checkout_not_descended_from_its_pin(full, monkeypatch):
    root = full["root"]
    _ahead(root, "web")
    checkout = _checkout(root, "api")
    git(checkout, "checkout", "-q", "--orphan", "feat/other")
    (checkout / "other.txt").write_text("other\n")
    git(checkout, "add", "other.txt")
    git(checkout, "commit", "-q", "-m", "unrelated")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    _bound(r, out, root)
    assert "api (checkout does not descend from the workspace's pin)" in out, out
    assert "pin api " not in out, out


def test_full_root_unborn_head_refuses(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    git(root, "symbolic-ref", "HEAD", "refs/heads/unborn")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == 2, out
    assert "root_commit_absent: 'HEAD' is not a commit of this full root" in out, out
    assert "nothing to pin" not in out and "pin api " not in out, out


def test_full_root_crafted_member_names_make_no_row(full, monkeypatch):
    """Section names git allows but whose path is crafted: this verb names `a/b` as unpinned and makes no row for it, a
    path that leaves the root refuses the whole read and writes no review, and nothing outside the root is read. (A
    name git disallows is refused by the set reader; see the rows below.)"""
    monkeypatch.setenv("COLUMNS", "2000")  # Rich folds a long temp path inside the refusal at the default width
    root = full["root"]
    _ahead(root, "api")
    base = (root / ".gitmodules").read_text()
    outside = full["tmp"] / "outside"
    bound = 0  # a rebind adds one review or rewrites the same one (both measured)
    for section, path in (("a/b", "ab"), ("esc", "../outside"), ("abs", str(outside))):
        (root / ".gitmodules").write_text(base + f'[submodule "{section}"]\n\tpath = {path}\n\turl = {full["urls"]["api"]}\n')
        git(root, "add", ".gitmodules")
        git(root, "commit", "-q", "-m", f"crafted {section}")
        r = gr2(root, monkeypatch, "review", "pin")
        out = _out(r)
        if path in ("../outside", str(outside)):  # a path that leaves the root refuses the whole read
            assert r.exit_code == 2 and f"member_path_outside_root: member {section!r} path {path!r}" in out, out
            assert len(_reviews(root)) == bound, _reviews(root)
        else:
            _bound(r, out, root, reviews=None)  # the api row
            assert max(bound, 1) <= len(_reviews(root)) <= bound + 1, _reviews(root)
            bound = len(_reviews(root))
            assert f"{section} (no gitlink at {path} in HEAD)" in out, out
        assert f"pin {section} " not in out, out
        assert not outside.exists()


PORT = "is not a name git allows for a submodule"
RUN = "does not make a valid member ref"
REFUSED = 2  # the pin's exit on every DeclaredSetError; measured by test_bad_member_name_exits_like_the_other_reader_refusals
BAD_NAMES = (("../x", PORT), ("..", PORT), ("a/../b", PORT), ("a\\..\\b", PORT), ("", PORT),
             ("/abs/x", RUN), (".lock/x", RUN), ("x.lock", RUN))


def _section(root: Path, url, name: str, field: str = "path") -> None:
    value = "x" if field == "path" else "s"
    quoted = name.replace("\\", "\\\\")  # a backslash in a section name is written escaped
    body = (root / ".gitmodules").read_text() + f'[submodule "{quoted}"]\n\t{field} = {value}\n\turl = {url}\n'
    (root / ".gitmodules").write_text(body)
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "a section")


def _objects(root: Path) -> str:
    return git(root, "count-objects", "-v")


@pytest.mark.parametrize("name, reason", BAD_NAMES, ids=[n or "empty" for n, _ in BAD_NAMES])
def test_bad_member_name_refuses_the_whole_read_and_writes_nothing(full, monkeypatch, name, reason):
    root = full["root"]
    _ahead(root, "api")
    _section(root, full["urls"]["api"], name)
    reviews, objects = _reviews(root), _objects(root)
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == REFUSED, out
    assert f"submodule_name_disallowed: member {name!r} in .gitmodules" in out, out
    assert reason in out, out
    assert "remove" not in out and "delete" not in out, out
    assert _reviews(root) == reviews and _objects(root) == objects, out
    assert not (root / ".grip").exists()


@pytest.mark.parametrize("name", ("x", "ok/nested"))
def test_allowed_member_name_still_pins(full, monkeypatch, name):
    root = full["root"]
    _ahead(root, "api")
    _section(root, full["urls"]["api"], name)
    r = gr2(root, monkeypatch, "review", "pin")
    _bound(r, _out(r), root)


@pytest.mark.parametrize("name, reason", (("../x", PORT), ("x.lock", RUN)))
def test_bad_name_on_a_set_line_alone_is_refused(full, monkeypatch, name, reason):
    """A section with ONLY a gr2-set line (no path, no url) never reaches the path/url loop; the set loop refuses it
    with the same words. x.lock is a name git's fsck allows, so only the set loop's check can refuse it."""
    root = full["root"]
    _ahead(root, "api")
    (root / ".gitmodules").write_text((root / ".gitmodules").read_text() + f'[submodule "{name}"]\n\tgr2-set = s\n')
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "a set line alone")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == REFUSED and f"submodule_name_disallowed: member {name!r}" in out and reason in out, out


def test_bad_member_name_exits_like_the_other_reader_refusals(full, monkeypatch):
    """The exit is measured, not read: a set_name_invalid refusal (the same DeclaredSetError branch) and a bad name
    exit alike."""
    root = full["root"]
    _ahead(root, "api")
    git(root, "config", "-f", ".gitmodules", "submodule.api.gr2-set", "bad name")
    git(root, "commit", "-q", "-am", "bad set name")
    r = gr2(root, monkeypatch, "review", "pin")
    assert "set_name_invalid" in _out(r) and r.exit_code == REFUSED, _out(r)
    git(root, "config", "-f", ".gitmodules", "--unset", "submodule.api.gr2-set")
    git(root, "commit", "-q", "-am", "no set")
    _section(root, full["urls"]["api"], "../x")
    assert gr2(root, monkeypatch, "review", "pin").exit_code == REFUSED


def test_printed_rename_step_recovers_the_pin(full, monkeypatch):
    """The step is parsed from the refusal as printed, not retyped; the one chosen name 'x' stands in for <new>."""
    root = full["root"]
    _ahead(root, "api")
    _section(root, full["urls"]["api"], "../x")
    out = _out(gr2(root, monkeypatch, "review", "pin"))
    step = re.search(r"with (git config -f \.gitmodules --rename-section .*?), then run the verb again", out)
    assert step, out
    argv = [word.replace("<new>", "x") for word in shlex.split(step.group(1))]
    assert subprocess.run(argv, cwd=root).returncode == 0
    assert "rewrite the commit that added the name" in _flat(out) and "reflog" not in out and "prune" not in out, out
    old_blob = git(root, "rev-parse", "HEAD:.gitmodules").strip()
    # The rewrite path: amend the commit that added the name (only .gitmodules; -a would stage api's moved gitlink).
    git(root, "commit", "-q", "--amend", "--no-edit", "--", ".gitmodules")
    assert subprocess.run(["git", "cat-file", "-e", old_blob], cwd=root).returncode == 0  # the old blob is still stored
    r = gr2(root, monkeypatch, "review", "pin")
    _bound(r, _out(r), root)
    assert "x" in declared_sets.read(root, "HEAD").members


def test_printed_rename_step_quotes_a_name_holding_a_quote(full, monkeypatch):
    """The refusal fires on hostile names, so a ' in the name stays inside one shell word of the printed step."""
    root = full["root"]
    _ahead(root, "api")
    _section(root, full["urls"]["api"], "../a'b")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert "submodule_name_disallowed:" in out and r.exit_code == REFUSED, out
    step = re.search(r"with (git config -f \.gitmodules --rename-section .*?), then run the verb again", out)
    assert step, out
    argv = ["git", "config", "-f", ".gitmodules", "--rename-section", "submodule.../a'b", "submodule.<new>"]
    assert shlex.split(step.group(1)) == argv, step.group(1)


def test_bad_name_in_an_older_commit_refuses_with_the_same_word(full, monkeypatch):
    """Renaming on top leaves the bad name in history: git's gitmodulesName error is caught into the reader's word."""
    root = full["root"]
    _ahead(root, "api")
    _section(root, full["urls"]["api"], "../x")
    argv = ["git", "config", "-f", ".gitmodules", "--rename-section", "submodule.../x", "submodule.x"]
    assert subprocess.run(argv, cwd=root).returncode == 0
    git(root, "commit", "-q", "-m", "renamed on top", "--", ".gitmodules")
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert "submodule_name_disallowed:" in out and r.exit_code == REFUSED, out
    assert "Traceback" not in out and (r.exception is None or isinstance(r.exception, SystemExit)), out
    assert "rewrite the commit that added the name" in _flat(out), out


def test_bad_name_on_a_merged_side_branch_refuses_naming_the_side_commit(full, monkeypatch):
    """A side branch adds the bad name and takes it back out, then merges --no-ff: the merge's .gitmodules matches its
    first parent, so the default history walk drops the side branch. The full history walk still reads the bad blob."""
    root = full["root"]
    _ahead(root, "api")
    base = git(root, "rev-parse", "HEAD").strip()
    trunk = git(root, "symbolic-ref", "--short", "HEAD").strip()
    git(root, "checkout", "-q", "-b", "side")
    _section(root, full["urls"]["api"], "../x")
    bad = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", base, "--", ".gitmodules")
    git(root, "commit", "-q", "-m", "took the section back out", "--", ".gitmodules")
    git(root, "checkout", "-q", trunk)
    git(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    assert bad not in git(root, "rev-list", "HEAD", "--", ".gitmodules").split()  # the default walk misses it
    reviews, objects = _reviews(root), _objects(root)
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == REFUSED and "submodule_name_disallowed: member '../x'" in out and bad[:12] in out, out
    assert _reviews(root) == reviews and _objects(root) == objects


def test_the_history_check_never_runs_fsck(full, monkeypatch):
    """The read's history check walks the commits that touch .gitmodules; it never pays for a whole-store fsck."""
    root = full["root"]
    _section(root, full["urls"]["api"], "../x")
    git(root, "config", "-f", ".gitmodules", "--rename-section", "submodule.../x", "submodule.x")
    git(root, "commit", "-q", "-m", "renamed on top", "--", ".gitmodules")
    seen, real = [], declared_sets.gitops.run
    monkeypatch.setattr(declared_sets.gitops, "run", lambda cwd, *args, **kw: seen.append(args) or real(cwd, *args, **kw))
    with pytest.raises(declared_sets.DeclaredSetError, match="submodule_name_disallowed"):
        declared_sets.read(root, "HEAD")
    assert any(a[:2] == ("rev-list", "--full-history") for a in seen) and not any("fsck" in a for a in seen), seen


def test_newline_in_a_section_name_is_unreadable_not_a_member(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    (root / ".gitmodules").write_text((root / ".gitmodules").read_text() + '[submodule "a\nb"]\n\tpath = x\n')
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "newline section")
    reviews, objects = _reviews(root), _objects(root)
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == REFUSED and "gitmodules_unreadable" in out, out
    assert _reviews(root) == reviews and _objects(root) == objects


def test_check_set_refuses_a_bad_member_name_with_the_same_word(full, monkeypatch):
    """A second verb through the same reader (check set --root-commit) refuses with the same word."""
    root = full["root"]
    _section(root, full["urls"]["api"], "../x")
    r = gr2(root, monkeypatch, "check", "set", str(root), "--root-commit", "HEAD", "--set", "s", "--", "true")
    out = _out(r)
    assert r.exit_code != 0 and "submodule_name_disallowed: member '../x'" in out and PORT in out, out


def _ghost(full) -> Path:
    """Declare a member "ghost" in .gitmodules with no gitlink in HEAD, and commit."""
    root = full["root"]
    gm = root / ".gitmodules"
    gm.write_text(gm.read_text() + f'[submodule "ghost"]\n\tpath = ghost\n\turl = {full["urls"]["api"]}\n')
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "declare ghost")
    return root


def test_full_root_members_flag_names_unpinned_member_once(full, monkeypatch):
    root = _ghost(full)
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin", "--members", "ghost")
    out = _out(r)
    assert "ghost (no gitlink at ghost in HEAD)" in out, out
    assert "ghost (not a member of this workspace)" not in out, out
    assert "pin ghost " not in out, out


def test_full_root_members_flag_excludes_unpinned_member(full, monkeypatch):
    root = _ghost(full)
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin", "--members", "api")
    out = _out(r)
    _bound(r, out, root)
    assert "ghost" not in out, out


def test_full_root_sync_wait_unchanged(full, monkeypatch):
    """workspace_members is not taught the full root: sync wait there refuses as it did before this change."""
    root = full["root"]
    r = gr2(root, monkeypatch, "sync", "wait", str(root), "--all-members", "--timeout", "1")
    out = _out(r)
    assert r.exit_code == 3, out
    assert "refused: the workspace declares no members" in out, out


def test_no_members_refuses_with_its_own_words(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    git(plain, "init", "-q", "-b", "main")
    r = gr2(plain, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code == 2, out
    assert "this workspace declares no members" in out, out
    assert OLD not in out, out


def test_full_root_bind_event_lives_in_the_git_dir(full, monkeypatch):
    """The bind still emits its event; at a full root the outbox and its lock sit under the root's git dir."""
    root = full["root"]
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin")
    _bound(r, _out(r), root)
    events = Path(git(root, "rev-parse", "--absolute-git-dir")) / "grip-events"
    assert events_mod._outbox_path(root) == events / "outbox.jsonl"
    assert "review.bound" in (events / "outbox.jsonl").read_text()
    assert (events / "outbox.lock").exists()


def test_full_root_list_and_merge_reach_their_own_checks_after_a_pin(full, monkeypatch):
    root = full["root"]
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin")
    _bound(r, _out(r), root)
    rid = _reviews(root)[0].rsplit("/", 1)[1]
    for args in (("review", "list"), ("review", "merge", rid)):
        out = _out(gr2(root, monkeypatch, *args))
        assert "has no .git/" not in out, out
        assert "does not read a full-mode root yet" in out, out


def test_full_root_read_events_after_a_pin_leaves_the_work_tree_clean(full, monkeypatch):
    """A reader's cursor resolves through the same events dir as the writer: reading at a full root writes nothing in
    its work tree either."""
    root = full["root"]
    _ahead(root, "api")
    r = gr2(root, monkeypatch, "review", "pin")
    _bound(r, _out(r), root)
    read = events_mod.read_events(root, "x")
    assert any(e.get("type") == "review.bound" for e in read)
    events_mod.ack_events(root, "x", read)  # reading never moves a cursor; the ack writes it
    events = Path(git(root, "rev-parse", "--absolute-git-dir")) / "grip-events"
    assert (events / "cursors" / "x.json").exists()
    assert not (root / ".grip").exists()
    assert git(root, "status", "--porcelain", "--ignored", "-uall", "--ignore-submodules=all") == ""


def test_native_root_outbox_path_unchanged(tmp_path):
    (tmp_path / "grip.toml").write_text("")
    assert events_mod._outbox_path(tmp_path) == tmp_path / ".grip" / "events" / "outbox.jsonl"


def test_native_root_cursors_path_unchanged(tmp_path):
    (tmp_path / "grip.toml").write_text("")
    assert events_mod._cursors_dir(tmp_path) == tmp_path / ".grip" / "events" / "cursors"


def _adopt(root: Path, monkeypatch) -> None:
    r = gr2(root, monkeypatch, "workspace", "init", ".")
    assert r.exit_code == 0, _out(r)
    assert (root / ".grip" / "workspace_spec.toml").is_file() and not (root / "grip.toml").exists()


def _legacy_bind_store(root: Path) -> Path:
    """What an older gr2 left at `<root>/.grip/.git`: a HEAD on one commit whose tree says it is a review bind."""
    store = root / ".grip" / ".git"
    git(root, "init", "-q", "--bare", str(store))

    def in_store(argv: list[str], text: str) -> str:
        return subprocess.run(["git", "--git-dir", str(store), *argv], input=text, check=True, capture_output=True,
                              text=True).stdout.strip()

    blob = in_store(["hash-object", "-w", "--stdin"], "gr2-review-bind/v2\n")
    inner = in_store(["mktree"], f"100644 blob {blob}\tschema\n")
    tree = in_store(["mktree"], f"040000 tree {inner}\t.grip\n")
    git(root, "--git-dir", str(store), "update-ref", "HEAD",
        git(root, "--git-dir", str(store), "commit-tree", tree, "-m", "grip review bind"))
    return store


def _root_state(root: Path) -> tuple[str, str]:
    return (git(root, "for-each-ref"), git(root, "status", "--porcelain", "--ignored", "-uall", "--ignore-submodules=all"))


def test_adopted_full_root_with_old_binds_refuses_the_pin_by_name_and_binds_nothing(full, monkeypatch):
    root = full["root"]
    _adopt(root, monkeypatch)
    store = _legacy_bind_store(root)
    old_refs = git(root, "--git-dir", str(store), "for-each-ref")
    old_head = git(root, "--git-dir", str(store), "rev-parse", "HEAD")
    old_head_bytes = (store / "HEAD").read_bytes()
    _ahead(root, MEMBERS[0])
    before = _root_state(root)
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code != 0, out
    assert "legacy_binds_left_behind: " in out and str(store) in out and str(root / ".git") in out, out
    # the next step is a NEW full root for new reviews: the old binds are the only copy and stay where they are
    assert "Those binds stay where they are (they are the only copy)" in out, out
    assert "start new reviews on a new full root: `gr2 store init <new dir> --mode full`." in out, out
    assert not any(word in out.lower() for word in ("aside", "move ", "rm ", "delete", "rename")), out
    assert _root_state(root) == before and _reviews(root) == []
    # what makes "stay where they are" true: the old store's refs and HEAD are byte-identical after the refusal; HEAD
    # is compared as the file's bytes, so a HEAD rewritten to a detached sha of the same commit is caught too
    assert git(root, "--git-dir", str(store), "for-each-ref") == old_refs
    assert (store / "HEAD").read_bytes() == old_head_bytes
    assert git(root, "--git-dir", str(store), "rev-parse", "HEAD") == old_head


@pytest.mark.parametrize("old_store", ["empty", "absent"])
def test_adopted_full_root_with_no_old_binds_passes_the_check(full, monkeypatch, old_store):
    root = full["root"]
    _adopt(root, monkeypatch)
    store = root / ".grip" / ".git"
    if old_store == "empty":
        git(root, "init", "-q", "--bare", str(store))  # no ref and an unborn HEAD: nothing left behind
    else:
        assert not store.exists()
    _ahead(root, MEMBERS[0])
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert "legacy_binds_left_behind" not in out and f"pin {MEMBERS[0]}" in out, out  # the check lets the pin through


def test_adopted_full_root_with_an_unreadable_old_store_refuses_the_pin(full, monkeypatch):
    root = full["root"]
    _adopt(root, monkeypatch)
    store = root / ".grip" / ".git"
    store.mkdir()
    (store / "HEAD").write_text("not a ref\n")
    (store / "config").write_text("[[[ not config\n")  # one git cannot read is not empty
    _ahead(root, MEMBERS[0])
    before = _root_state(root)
    r = gr2(root, monkeypatch, "review", "pin")
    out = _out(r)
    assert r.exit_code != 0, out
    assert "legacy_binds_left_behind: " in out and str(store) in out, out
    assert _root_state(root) == before and _reviews(root) == []

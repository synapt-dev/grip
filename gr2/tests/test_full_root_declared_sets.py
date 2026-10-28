"""A full root's declared sets are read from its root COMMIT's .gitmodules (spec section 14.1, as amended: at most ONE
`gr2-set` line per member section), never from the worktree, in three steps: the root commit must be here (else
refuse, never "no sets"); no .gitmodules means no members and no sets; then `git config --blob ... --get-regexp`,
where rc 1 is "no sets" and any other nonzero refuses. Section 14.4: a set left with one member is named, and a root
holding both grip.toml and gr2-set lines is refused by name.

The roots here are griplink roots (`gr2 store init --mode full`, then `gr2 add`), the shape section 14 is about, and
each member has its own content so a row can tell the members apart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from gr2.python_cli import declared_sets
from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.test_griplink_cli import ENV, git, gr2

MEMBERS = ("api", "web")


@pytest.fixture
def full(tmp_path: Path, monkeypatch) -> dict:
    """A griplink root R with two members whose commits differ, and nothing declared yet."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    urls = {}
    for m in MEMBERS:
        src = tmp_path / f"src-{m}"
        src.mkdir()
        git(src, "init", "-q", "-b", "main")
        (src / f"{m}.txt").write_text(f"{m} v1\n")
        git(src, "add", f"{m}.txt")
        git(src, "commit", "-q", "-m", f"{m} base")
        urls[m] = tmp_path / f"{m}.git"
        git(tmp_path, "clone", "-q", "--bare", str(src), str(urls[m]))
    assert gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", "R").exit_code == 0
    root = tmp_path / "R"
    for m in MEMBERS:
        r = gr2(root, monkeypatch, "add", str(urls[m]), m)
        assert r.exit_code == 0, r.stderr
    return {"tmp": tmp_path, "root": root, "urls": urls}


def _declare(root: Path, *lines: tuple[str, str], quoted: bool = False) -> str:
    """Write gr2-set lines into .gitmodules by hand (no verb writes them yet) and commit; return the root commit."""
    gm = root / ".gitmodules"
    text = gm.read_text()
    for member, value in lines:
        head = f'[submodule "{member}"]\n'
        assert head in text, (member, text)
        shown = f'"{value}"' if quoted else value
        text = text.replace(head, f"{head}\tgr2-set = {shown}\n", 1)
    gm.write_text(text)
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "declare")
    return git(root, "rev-parse", "HEAD")


def _refused(root: Path, revision: str = "HEAD") -> str:
    with pytest.raises(declared_sets.DeclaredSetError) as exc:
        declared_sets.read(root, revision)
    return str(exc.value)


# R1: the set is read from the root commit, with each member's path and url from .gitmodules
def test_a_full_root_reads_its_set_from_the_root_commit(full):
    root = full["root"]
    commit = _declare(root, ("api", "billing"), ("web", "billing"))
    d = declared_sets.read(root, commit)
    assert d.source == "gitmodules"
    assert [(s.name, s.members) for s in d.sets] == [("billing", ("api", "web"))]
    assert d.members["api"] == {"path": "api.griplink/001.2_pin", "remote": str(full["urls"]["api"])}
    assert d.members["web"] == {"path": "web.griplink/001.2_pin", "remote": str(full["urls"]["web"])}
    pins, missing = declared_sets.gitlinks(root, commit, d, d.sets[0])
    assert missing == []
    assert pins == {m: git(root / m, "rev-parse", "HEAD") for m in MEMBERS}
    assert pins["api"] != pins["web"]  # distinct members, so the pins cannot agree by accident


# R2: a root commit that is not here refuses by name; it is never read as "no sets" (14.1 step 1)
def test_a_root_commit_not_here_refuses_and_is_never_read_as_no_sets(full):
    root = full["root"]
    _declare(root, ("api", "billing"), ("web", "billing"))
    absent = "0" * 39 + "1"
    message = _refused(root, absent)
    assert message.startswith(f"root_commit_absent: {absent!r} is not a commit of this full root"), message


# R3: a full root whose commit has no .gitmodules has no sets, and that is not a refusal
def test_a_full_root_with_no_gitmodules_has_no_sets(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert gr2(tmp_path, monkeypatch, "store", "init", "--mode", "full", "R").exit_code == 0
    root = tmp_path / "R"
    assert git(root, "ls-tree", "--name-only", "HEAD", "--", ".gitmodules") == ""
    d = declared_sets.read(root)
    assert d.sets == () and d.members == {} and d.source == "gitmodules"


# R4: a .gitmodules git cannot parse refuses naming the rc, never "no sets"
def test_a_gitmodules_git_cannot_parse_refuses_and_is_never_read_as_no_sets(full):
    root = full["root"]
    (root / ".gitmodules").write_text((root / ".gitmodules").read_text() + "[submodule\n")
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "broken")
    message = _refused(root)
    assert message.startswith("gitmodules_unreadable: git config --blob "), message


# R5: a set name outside [A-Za-z0-9._-] is named; a plain one is taken
@pytest.mark.parametrize("bad", ["a b", "c;d"])
def test_a_set_name_outside_the_charset_is_refused_by_name(full, bad):
    root = full["root"]
    _declare(root, ("api", bad), ("web", bad), quoted=True)
    message = _refused(root)
    assert message.startswith(f"set_name_invalid: member 'api' names set {bad!r}"), message


def test_a_plain_set_name_is_taken(full):
    root = full["root"]
    _declare(root, ("api", "a.b_c-1"), ("web", "a.b_c-1"))
    assert [s.name for s in declared_sets.read(root).sets] == ["a.b_c-1"]


# R6: a set left with one member is named, never silently passed (14.4)
def test_a_set_with_one_member_is_refused_by_name(full):
    root = full["root"]
    _declare(root, ("api", "billing"))
    message = _refused(root)
    assert message.startswith("set_with_one_member: set 'billing' names only 'api'"), message


# R7: a member section with two gr2-set lines is refused, in the words the store-root reader uses
def test_a_member_with_two_set_lines_is_refused_in_the_store_readers_words(full):
    root = full["root"]
    _declare(root, ("api", "billing"), ("api", "auth"), ("web", "billing"))
    message = _refused(root)
    assert message == declared_sets._two_sets("api", "auth", "billing"), message
    with pytest.raises(declared_sets.DeclaredSetError) as compat:
        declared_sets.parse({"members": [{"name": "api", "path": "api"}],
                             "sets": [{"name": "auth", "members": ["api"]}, {"name": "billing", "members": ["api"]}]})
    assert str(compat.value).endswith(declared_sets.ONE_SET_PER_MEMBER) and message.endswith(declared_sets.ONE_SET_PER_MEMBER)


# R8: a root commit holding grip.toml and gr2-set lines is refused by name (14.4)
def test_a_root_holding_both_shapes_is_refused_by_name(full):
    root = full["root"]
    (root / "grip.toml").write_text("")
    git(root, "add", "grip.toml")
    git(root, "commit", "-q", "-m", "a grip.toml")
    commit = _declare(root, ("api", "billing"), ("web", "billing"))
    message = _refused(root)
    assert message.startswith(f"both_set_shapes: the commit {commit[:12]} holds grip.toml and gr2-set lines"), message


# R9: a gr2-set line only in the worktree is not read; the committed lines are
def test_a_worktree_only_set_line_is_not_read(full):
    root = full["root"]
    commit = _declare(root, ("api", "billing"), ("web", "billing"))
    gm = root / ".gitmodules"
    gm.write_text(gm.read_text().replace("gr2-set = billing", "gr2-set = ops"))  # uncommitted
    assert [(s.name, s.members) for s in declared_sets.read(root, commit).sets] == [("billing", ("api", "web"))]


# R10: `gr2 check set --root-commit` runs on a full root's declared set and publishes one record per member
def test_check_set_root_commit_runs_on_a_full_roots_set(full, monkeypatch):
    root = full["root"]
    commit = _declare(root, ("api", "billing"), ("web", "billing"))
    monkeypatch.chdir(root)
    out = make_cli_runner().invoke(app, ["check", "set", str(root), "--root-commit", commit, "--set", "billing",
                                         "--", sys.executable, "-c", "pass"], catch_exceptions=False)
    assert out.exit_code == 0, out.output
    body = json.loads(out.stdout.splitlines()[0])
    assert body["set"] == "billing" and body["root_commit"] == commit and body["result"] == "pass", body
    assert sorted(m["member"] for m in body["members"]) == list(MEMBERS)
    assert {m["member"]: m["head"] for m in body["members"]} == {m: git(root / m, "rev-parse", "HEAD") for m in MEMBERS}


# R11: a gr2-set line on a section with no path is refused by name; a set never names a member it cannot pin
def test_a_set_line_for_a_member_with_no_path_is_refused_by_name(full):
    root = full["root"]
    gm = root / ".gitmodules"
    gm.write_text(gm.read_text() + '[submodule "ghost"]\n\tgr2-set = billing\n')
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "a set line with no path")
    message = _refused(root)
    assert message.startswith("set_member_unknown: set 'billing' names 'ghost', which has no path"), message


# R12: a section named with a space is a member (git allows the name); its set reads whole, key never cut at the space
def test_a_section_named_with_a_space_reads_its_set(full):
    root = full["root"]
    gm = root / ".gitmodules"
    gm.write_text(gm.read_text() + '[submodule "my api"]\n\tpath = myapi\n\turl = https://example.invalid/my-api.git\n'
                  '\tgr2-set = billing\n')
    _declare(root, ("web", "billing"))
    d = declared_sets.read(root)
    assert [(s.name, s.members) for s in d.sets] == [("billing", ("my api", "web"))]
    assert d.members["my api"]["path"] == "myapi"


# R13: a store root reads grip.toml even when the .gitmodules beside it does not parse
def test_a_store_root_with_a_malformed_gitmodules_still_reads_its_grip_toml(full):
    root = full["root"]
    (root / "grip.toml").write_text("")
    gm = root / ".gitmodules"
    gm.write_text(gm.read_text() + "[submodule\n")
    git(root, "add", "grip.toml", ".gitmodules")
    git(root, "commit", "-q", "-m", "a grip.toml beside a broken .gitmodules")
    d = declared_sets.read(root)
    assert d.source == "grip.toml" and d.sets == ()


# R14: a section with a url and a gr2-set line but no path is refused by name, never taken as a member
def test_a_set_line_for_a_section_with_a_url_and_no_path_is_refused_by_name(full):
    root = full["root"]
    gm = root / ".gitmodules"
    gm.write_text(gm.read_text() + '[submodule "ghost"]\n\turl = https://example.invalid/ghost.git\n\tgr2-set = billing\n')
    git(root, "add", ".gitmodules")
    git(root, "commit", "-q", "-m", "a set line with a url and no path")
    message = _refused(root)
    assert message.startswith("set_member_unknown: set 'billing' names 'ghost', which has no path"), message

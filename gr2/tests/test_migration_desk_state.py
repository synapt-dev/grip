"""TDD spec: migrate-gr1's NOT ADOPTED receipt says what each desk IS, and never lists the root.

The NOT ADOPTED receipt used to list every declared worktree, one row each, whatever
was on disk: a clean desk, an ahead desk, a dirty desk and a desk whose directory does not exist all
printed the same line, and the unit whose worktree is "main" (declared at "." -- the root, which IS
the adopted workspace) was listed as not adopted.

Contract pinned here:
  * the root unit (worktree "main") is never listed, and is not counted;
  * every other desk row carries its STATE, read from the desk's own checkouts without changing them
    (clean | ahead N | dirty N | missing | no-checkout), and the command that is the next step;
  * state is read-only: nothing under a desk changes;
  * the human render and the summary JSON carry the same state.

Fixtures are REAL git repos (a bare origin made with `clone --bare`, a root repo, desks whose repo is a
linked worktree of the root's, the shape gr1 wrote), because the old rows used bare directories and so
could never tell one desk shape from another.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from gr2.python_cli.migration import migrate_gr1_workspace, render_migration

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@example.invalid",
    "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {done.stderr}"
    return done.stdout.strip()


def _gripspace(tmp_path: Path, agents: str) -> tuple[Path, Path]:
    """A gr1 gripspace at tmp_path/ws with a real repo `alpha` and a bare origin. Returns (root, alpha)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "README.md").write_text("alpha v1\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "alpha: first")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    root = tmp_path / "ws"
    (root / ".gitgrip" / "spaces" / "main").mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(origin), str(root / "alpha"))
    (root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").write_text(
        yaml.dump({
            "version": 2,
            "manifest": {"url": str(tmp_path / "gripspace.git")},
            "repos": {"alpha": {"url": str(origin), "path": "./alpha", "revision": "main"}},
        })
    )
    (root / ".gitgrip" / "agents.toml").write_text(agents)
    return root, root / "alpha"


def _linked_desk(tmp_path: Path, alpha: Path, name: str) -> Path:
    """A gr1 desk beside the root: <parent>/<name>/alpha is a linked worktree of the root's alpha."""
    desk = tmp_path / name
    desk.mkdir()
    _git(alpha, "worktree", "add", "-q", "-b", f"br-{name}", str(desk / "alpha"), "main")
    return desk


def _row(payload: dict, unit: str) -> dict:
    rows = [r for r in payload["not_adopted"] if r["unit"] == unit]
    assert len(rows) == 1, f"expected one NOT ADOPTED row for {unit!r}, got {payload['not_adopted']}"
    return rows[0]


def _agents(**units: str) -> str:
    return "".join(f'[agents.{u}]\nworktree = "{w}"\nchannel = "dev"\n\n' for u, w in units.items())


def _fingerprint(desk: Path) -> str:
    h = hashlib.sha256()
    for dp, dn, fn in os.walk(desk):
        dn.sort()
        for f in sorted(fn):
            p = Path(dp) / f
            h.update(f"{p.relative_to(desk)}:{p.stat().st_size}:".encode())
            if not p.is_symlink():
                h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------
# the root unit
# ---------------------------------------------------------------------------------------------------

def test_the_root_unit_is_never_listed_as_not_adopted(tmp_path: Path) -> None:
    """worktree = "main" is declared at "." (the root, the adopted workspace). It is a unit, not a desk."""
    root, alpha = _gripspace(tmp_path, _agents(lead="main", other="ws-other"))
    _linked_desk(tmp_path, alpha, "ws-other")
    payload = migrate_gr1_workspace(root)
    assert "lead" in payload["units"]
    assert [r["unit"] for r in payload["not_adopted"]] == ["other"], payload["not_adopted"]
    rendered = render_migration(payload)
    assert "- lead:" not in rendered, rendered
    assert "1 gr1 worktree(s)" in rendered, rendered


def test_control_a_workspace_with_only_the_root_unit_has_no_not_adopted_section(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(lead="main"))
    payload = migrate_gr1_workspace(root)
    assert payload["not_adopted"] == []
    assert "NOT ADOPTED" not in render_migration(payload)


# ---------------------------------------------------------------------------------------------------
# one row per state, each pinned
# ---------------------------------------------------------------------------------------------------

def test_a_clean_linked_desk_reads_clean_and_names_convert_clone(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(cleandesk="ws-clean"))
    desk = _linked_desk(tmp_path, alpha, "ws-clean")
    row = _row(migrate_gr1_workspace(root), "cleandesk")
    assert row["state"] == "clean", row
    assert f"gr2 workspace convert-clone {desk / 'alpha'}" in row["next"], row


def test_an_ahead_desk_reads_ahead_n_and_says_to_push_first(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(aheaddesk="ws-ahead"))
    desk = _linked_desk(tmp_path, alpha, "ws-ahead")
    (desk / "alpha" / "ahead.txt").write_text("local only\n")
    _git(desk / "alpha", "add", "ahead.txt")
    _git(desk / "alpha", "commit", "-q", "-m", "desk: local commit")
    row = _row(migrate_gr1_workspace(root), "aheaddesk")
    assert row["state"] == "ahead 1", row
    assert "push" in row["next"].lower(), row
    assert "convert-clone" in row["next"], row


def test_a_dirty_desk_reads_dirty_n_and_says_commit_or_stash_before_converting(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(dirtydesk="ws-dirty"))
    desk = _linked_desk(tmp_path, alpha, "ws-dirty")
    (desk / "alpha" / "README.md").write_text("alpha v1\nedited\n")  # a tracked change
    (desk / "alpha" / "scratch.txt").write_text("untracked\n")       # an untracked file
    row = _row(migrate_gr1_workspace(root), "dirtydesk")
    assert row["state"] == "dirty 2", row
    assert "commit or stash" in row["next"], row
    # convert-clone REFUSES a dirty tree, so the row must not offer it as the immediate step
    assert not row["next"].startswith("gr2 workspace convert-clone"), row


def test_a_declared_desk_with_no_directory_reads_missing_and_offers_no_conversion(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(gonedesk="ws-gone"))
    row = _row(migrate_gr1_workspace(root), "gonedesk")
    assert row["state"] == "missing", row
    assert "convert-clone" not in row["next"], row
    assert "absent" in row["next"], row


def test_a_desk_whose_repo_is_already_an_own_clone_has_nothing_to_convert(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(owndesk="ws-own"))
    desk = tmp_path / "ws-own"
    desk.mkdir()
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(desk / "alpha"))
    row = _row(migrate_gr1_workspace(root), "owndesk")
    assert row["state"] == "clean", row
    assert "convert-clone" not in row["next"], row
    assert "own clone" in row["next"], row


def test_a_desk_directory_with_no_checkout_in_it_is_named_as_such(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(emptydesk="ws-empty"))
    (tmp_path / "ws-empty").mkdir()
    row = _row(migrate_gr1_workspace(root), "emptydesk")
    assert row["state"] == "no-checkout", row


def test_an_ahead_and_dirty_desk_reports_both_in_a_fixed_order(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(both="ws-both"))
    desk = _linked_desk(tmp_path, alpha, "ws-both")
    (desk / "alpha" / "c.txt").write_text("c\n")
    _git(desk / "alpha", "add", "c.txt")
    _git(desk / "alpha", "commit", "-q", "-m", "desk: commit")
    (desk / "alpha" / "README.md").write_text("alpha v1\nedited\n")
    row = _row(migrate_gr1_workspace(root), "both")
    assert row["state"] == "dirty 1, ahead 1", row
    assert "commit or stash" in row["next"], row


def test_a_detached_head_desk_says_to_check_out_a_branch_before_converting(tmp_path: Path) -> None:
    """convert-clone refuses a detached HEAD, so the row must not offer it first."""
    root, alpha = _gripspace(tmp_path, _agents(detached="ws-detached"))
    desk = _linked_desk(tmp_path, alpha, "ws-detached")
    _git(desk / "alpha", "checkout", "-q", "--detach")
    row = _row(migrate_gr1_workspace(root), "detached")
    assert row["state"] == "clean", row
    assert "check out a branch first" in row["next"], row
    assert not row["next"].startswith("gr2 workspace convert-clone"), row


def test_a_symlinked_git_dir_says_to_reclone_not_to_convert(tmp_path: Path) -> None:
    """A .git that is a symlink into another clone's git state is a different violation with a different
    fix; convert-clone refuses it, so the row offers no conversion."""
    root, alpha = _gripspace(tmp_path, _agents(linkdesk="ws-link"))
    desk = tmp_path / "ws-link"
    (desk / "alpha").mkdir(parents=True)
    (desk / "alpha" / ".git").symlink_to(alpha / ".git")
    row = _row(migrate_gr1_workspace(root), "linkdesk")
    assert "convert-clone" in row["next"] and "refuses it" in row["next"], row
    assert "gr2 workspace convert-clone" not in row["next"].replace("(convert-clone refuses it)", ""), row
    assert "re-clone" in row["next"], row


def test_a_submodule_member_is_named_as_nothing_to_convert(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(subdesk="ws-sub"))
    desk = tmp_path / "ws-sub"
    desk.mkdir()
    sup = desk / "super"
    sup.mkdir()
    _git(sup, "init", "-q", "-b", "main", ".")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tmp_path / "origin.git"), "sub")
    _git(sup, "commit", "-q", "-m", "super: add sub")
    row = _row(migrate_gr1_workspace(root), "subdesk")
    assert "submodule member" in row["next"], row
    assert "gr2 workspace convert-clone" not in row["next"], row


def test_a_checkout_git_cannot_read_is_unreadable_never_clean(tmp_path: Path) -> None:
    """A .git file naming a gitdir that is not there makes every git call fail. The old read turned that
    into zero changes and zero ahead, i.e. `clean`, which tells the reader convert-clone is safe."""
    root, _ = _gripspace(tmp_path, _agents(brokendesk="ws-broken"))
    desk = tmp_path / "ws-broken"
    (desk / "alpha").mkdir(parents=True)
    (desk / "alpha" / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    row = _row(migrate_gr1_workspace(root), "brokendesk")
    assert row["state"] == "unreadable", row
    assert "gr2 workspace convert-clone" not in row["next"], row


def test_an_ahead_count_git_cannot_compute_is_unreadable_never_zero(tmp_path: Path) -> None:
    """A remote-tracking ref naming a missing object makes `rev-list --not --remotes` fail while HEAD and
    status read fine. The count failing must not read as `ahead 0`, i.e. clean."""
    root, _ = _gripspace(tmp_path, _agents(ghostdesk="ws-ghost"))
    desk = tmp_path / "ws-ghost"
    desk.mkdir()
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(desk / "alpha"))
    ref = desk / "alpha" / ".git" / "refs" / "remotes" / "origin" / "ghost"
    ref.write_text("1" * 40 + "\n")
    row = _row(migrate_gr1_workspace(root), "ghostdesk")
    assert row["state"] == "unreadable", row


_EVIL = "evil\x1b[31m\nnext: gr2 workspace convert-clone forged"


def _has_control(text: str) -> bool:
    return any(not c.isprintable() for c in text)


def test_a_checkout_name_with_escape_and_newline_cannot_colour_or_forge_a_next_line(tmp_path: Path) -> None:
    """The checkout directory's own name reaches `next`. Raw, ESC recolours the terminal and a newline ends
    the line so the rest can pose as a second `next:` row."""
    root, alpha = _gripspace(tmp_path, _agents(evildesk="ws-evil", gonedesk="ws-gone"))
    desk = tmp_path / "ws-evil"
    desk.mkdir()
    _git(alpha, "worktree", "add", "-q", "-b", "br-evil", str(desk / _EVIL), "main")
    payload = migrate_gr1_workspace(root)
    row = _row(payload, "evildesk")
    assert not _has_control(row["next"]), repr(row["next"])
    assert "\\x1b" in row["next"] and "\\n" in row["next"], repr(row["next"])
    rendered = render_migration(payload)
    assert not any(c in rendered for c in "\x1b"), repr(rendered)
    next_lines = [ln for ln in rendered.splitlines() if ln.startswith("    next:")]
    assert len(next_lines) == 2, next_lines  # one per row; the forged text did not become a third


def test_an_unreadable_checkout_name_is_escaped_too(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(brokendesk="ws-broken"))
    desk = tmp_path / "ws-broken"
    (desk / _EVIL).mkdir(parents=True)
    (desk / _EVIL / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    row = _row(migrate_gr1_workspace(root), "brokendesk")
    assert row["state"] == "unreadable", row
    assert not _has_control(row["next"]), repr(row["next"])


def test_a_manifest_worktree_name_with_a_control_character_is_refused_upstream(tmp_path: Path) -> None:
    """Why the unit and worktree names need no escaping in the render: the manifest refuses them."""
    root, _ = _gripspace(tmp_path, '[agents."odd"]\nworktree = "ws-odd\\u001b[31m"\nchannel = "dev"\n')
    with pytest.raises(SystemExit, match="control characters"):
        migrate_gr1_workspace(root)


def test_a_desk_with_a_stash_entry_is_not_clean_and_says_it_stays_behind(tmp_path: Path) -> None:
    """A stash belongs to the repository. After convert-clone the clone's stash list is empty and the entry
    stays in the root repo, so `clean` here would tell the reader the conversion loses nothing."""
    root, alpha = _gripspace(tmp_path, _agents(stashdesk="ws-stash"))
    desk = _linked_desk(tmp_path, alpha, "ws-stash")
    (desk / "alpha" / "README.md").write_text("alpha v1\nstashed\n")
    _git(desk / "alpha", "stash", "push", "-q")
    row = _row(migrate_gr1_workspace(root), "stashdesk")
    assert row["state"] == "stash 1", row
    assert "stash entry belongs to the repository and stays in the root repo" in row["next"], row
    assert "git stash apply" in row["next"] and "apply keeps the entry" in row["next"], row
    assert "gr2 workspace convert-clone" in row["next"], row


def _stash_desk(tmp_path: Path):
    root, alpha = _gripspace(tmp_path, _agents(stashdesk="ws-stash"))
    desk = _linked_desk(tmp_path, alpha, "ws-stash")
    (desk / "alpha" / "README.md").write_text("alpha v1\nstashed work\n")
    _git(desk / "alpha", "stash", "push", "-q")
    return root, desk / "alpha"


def test_the_printed_stash_steps_in_that_order_convert_through_the_real_verb(tmp_path: Path) -> None:
    """The row says apply, commit, then convert-clone. Follow exactly that through gr2's own convert: it
    converts, and the commit is the clone's HEAD. A line that said `apply, then convert` was false: apply
    leaves the tree dirty and convert-clone refuses it."""
    from gr2.prototypes import repo_maintenance_prototype as repo_proto

    root, checkout = _stash_desk(tmp_path)
    text = _row(migrate_gr1_workspace(root), "stashdesk")["next"]
    assert text.index("git stash apply") < text.index("commit") < text.index("gr2 workspace convert-clone"), text

    _git(checkout, "stash", "apply", "-q")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "carry the stashed work")
    repo_proto.convert_worktree_to_clone(checkout)

    assert (checkout / ".git").is_dir()
    assert "stashed work" in (checkout / "README.md").read_text()
    assert _git(checkout, "log", "-1", "--format=%s") == "carry the stashed work"


def test_twin_apply_without_commit_is_refused_by_convert_clone(tmp_path: Path) -> None:
    """The twin of the row above: the sentence needs `commit` because without it the verb refuses."""
    from gr2.prototypes import repo_maintenance_prototype as repo_proto

    _root, checkout = _stash_desk(tmp_path)
    _git(checkout, "stash", "apply", "-q")
    with pytest.raises(repo_proto.ConvertCloneError, match="dirty"):
        repo_proto.convert_worktree_to_clone(checkout)


def test_linked_checkouts_of_one_repo_count_its_stash_once(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(twodesk="ws-two"))
    desk = tmp_path / "ws-two"
    desk.mkdir()
    for name in ("a", "b"):
        _git(alpha, "worktree", "add", "-q", "-b", f"br-two-{name}", str(desk / name), "main")
    (desk / "a" / "README.md").write_text("alpha v1\nstashed\n")
    _git(desk / "a", "stash", "push", "-q")
    row = _row(migrate_gr1_workspace(root), "twodesk")
    assert row["state"] == "stash 1", row  # both checkouts see the one stash; it is not 2


def test_a_stash_list_git_cannot_read_is_unreadable_never_clean(tmp_path: Path) -> None:
    """A refs/stash naming a missing object fails `stash list` alone: status and the ahead count read fine,
    so without this the desk would read clean while a stash it cannot see sits in the repo."""
    root, _ = _gripspace(tmp_path, _agents(ghoststash="ws-gs"))
    desk = tmp_path / "ws-gs"
    desk.mkdir()
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(desk / "alpha"))
    (desk / "alpha" / ".git" / "refs" / "stash").write_text("1" * 40 + "\n")
    row = _row(migrate_gr1_workspace(root), "ghoststash")
    assert row["state"] == "unreadable", row


def test_a_symlinked_git_path_with_escape_and_newline_is_escaped(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(linkdesk="ws-link"))
    desk = tmp_path / "ws-link"
    (desk / _EVIL).mkdir(parents=True)
    (desk / _EVIL / ".git").symlink_to(alpha / ".git")
    row = _row(migrate_gr1_workspace(root), "linkdesk")
    assert "re-clone" in row["next"], row
    assert not _has_control(row["next"]), repr(row["next"])


def test_a_submodule_member_path_with_escape_and_newline_is_escaped(tmp_path: Path) -> None:
    root, _ = _gripspace(tmp_path, _agents(subdesk="ws-sub"))
    sup = tmp_path / "ws-sub" / _EVIL
    sup.mkdir(parents=True)
    _git(sup, "init", "-q", "-b", "main", ".")
    _git(sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tmp_path / "origin.git"), "sub")
    _git(sup, "commit", "-q", "-m", "super: add sub")
    row = _row(migrate_gr1_workspace(root), "subdesk")
    assert "submodule member" in row["next"], row
    assert not _has_control(row["next"]), repr(row["next"])


_DESTRUCTIVE = ("drop", "pop", "clear", "delete", "remove", "rm ", "--force", "reset --hard", "git clean", "discard")


def test_no_next_step_in_any_state_advises_a_destructive_act(tmp_path: Path) -> None:
    """Advice a reader runs must never delete work: a stash `drop`/`pop`, a `remove`, a force. Every state
    this module can print is built in one workspace and every `next` is scanned."""
    root, alpha = _gripspace(tmp_path, _agents(
        cleandesk="ws-clean", aheaddesk="ws-ahead", dirtydesk="ws-dirty", stashdesk="ws-stash",
        detached="ws-det", linkdesk="ws-link", brokendesk="ws-broken", gonedesk="ws-gone", emptydesk="ws-empty",
    ))
    _linked_desk(tmp_path, alpha, "ws-clean")
    a = _linked_desk(tmp_path, alpha, "ws-ahead")
    (a / "alpha" / "a.txt").write_text("a\n")
    _git(a / "alpha", "add", "a.txt")
    _git(a / "alpha", "commit", "-q", "-m", "a")
    d = _linked_desk(tmp_path, alpha, "ws-dirty")
    (d / "alpha" / "README.md").write_text("alpha v1\nedited\n")
    s = _linked_desk(tmp_path, alpha, "ws-stash")
    (s / "alpha" / "README.md").write_text("alpha v1\nstashed\n")
    _git(s / "alpha", "stash", "push", "-q")
    _git(_linked_desk(tmp_path, alpha, "ws-det") / "alpha", "checkout", "-q", "--detach")
    (tmp_path / "ws-link" / "alpha").mkdir(parents=True)
    (tmp_path / "ws-link" / "alpha" / ".git").symlink_to(alpha / ".git")
    (tmp_path / "ws-broken" / "alpha").mkdir(parents=True)
    (tmp_path / "ws-broken" / "alpha" / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    (tmp_path / "ws-empty").mkdir()
    payload = migrate_gr1_workspace(root)
    states = {r["unit"]: r["state"] for r in payload["not_adopted"]}
    assert len(states) == 9 and "unreadable" in states.values() and "missing" in states.values(), states
    for row in payload["not_adopted"]:
        low = row["next"].lower()
        hits = [w for w in _DESTRUCTIVE if w in low]
        assert not hits, (row["unit"], hits, row["next"])


# ---------------------------------------------------------------------------------------------------
# read-only, and the same state on every surface
# ---------------------------------------------------------------------------------------------------

def test_the_migration_changes_nothing_under_any_desk(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(a="ws-a", b="ws-b"))
    a = _linked_desk(tmp_path, alpha, "ws-a")
    b = _linked_desk(tmp_path, alpha, "ws-b")
    (b / "alpha" / "README.md").write_text("alpha v1\nedited\n")
    (b / "alpha" / "u.txt").write_text("u\n")
    before = (_fingerprint(a), _fingerprint(b))
    migrate_gr1_workspace(root)
    assert (_fingerprint(a), _fingerprint(b)) == before


def test_render_and_summary_json_carry_each_desks_state(tmp_path: Path) -> None:
    root, alpha = _gripspace(tmp_path, _agents(cleandesk="ws-clean", gonedesk="ws-gone"))
    _linked_desk(tmp_path, alpha, "ws-clean")
    payload = migrate_gr1_workspace(root)
    rendered = render_migration(payload)
    assert "cleandesk: ws-clean (clean)" in rendered, rendered
    assert "gonedesk: ws-gone (missing)" in rendered, rendered
    assert "contents were not inspected" not in rendered, rendered
    summary = json.loads((root / ".grip" / "migrations" / "gr1" / "migration-summary.json").read_text())
    assert {r["unit"]: r["state"] for r in summary["not_adopted"]} == {"cleandesk": "clean", "gonedesk": "missing"}


def test_the_ahead_advice_does_not_promise_a_post_conversion_count(tmp_path: Path) -> None:
    """What `ahead` reads AFTER convert-clone is a separate question. This row's advice is about BEFORE: it must
    not tell the reader the converted clone will show the same count, or that it will read as pushed."""
    root, alpha = _gripspace(tmp_path, _agents(aheaddesk="ws-ahead"))
    desk = _linked_desk(tmp_path, alpha, "ws-ahead")
    (desk / "alpha" / "x.txt").write_text("x\n")
    _git(desk / "alpha", "add", "x.txt")
    _git(desk / "alpha", "commit", "-q", "-m", "x")
    text = _row(migrate_gr1_workspace(root), "aheaddesk")["next"].lower()
    for forbidden in ("after conversion", "after the conversion", "will read", "up to date", "will show"):
        assert forbidden not in text, text

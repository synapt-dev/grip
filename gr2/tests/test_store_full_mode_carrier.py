"""Full mode: a member's gitlink is its own commit, and what else the record says sits in a sidecar.

THE SHAPE: in a workspace whose grip.toml says `mode = "full"`, the grip tree holds a mode-160000 entry per member that
points at the member's own commit (the pin), so plain `git status` agrees with `gr2 store status`. What the gitlink
and grip.toml do not say sits under `.grip/fields/<name>/` in the same tree (the schema, the kind, a layer for a pin no
remote holds, the records about the pin). A plain clone, a shallow clone and a plain pull carry every record, because
it is in the grip commit's own tree, and the grip history holds grip commits only. (Before the sidecar, the gitlink
named a field-commit carried as a parent; test_store_full_mode_layers keeps witnesses of that shape.)

Smallest proof first: one covered pin per member. A pin that is on no remote is carried by a layer (see
test_store_full_mode_layers).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import member_field_tree as mft
from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _full_store(tmp_path: Path) -> Path:
    """Two members cloned side by side, a store init, `mode = "full"` written by hand, and one recorded root commit."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text('mode = "full"\n' + text, encoding="utf-8")
    first = gr2(root, "commit", "-m", "first")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    return root


def _gitlink(root: Path, member: str, rev: str = "HEAD") -> str:
    return git(root, "ls-tree", rev, "--", member).stdout.split()[2]


def _member_head(root: Path, member: str) -> str:
    return git(root / member, "rev-parse", "HEAD").stdout.strip()


def _advance(root: Path, member: str, *, push: bool) -> str:
    work = root / member
    marker = work / f"{member}-{len(list(work.glob('*.txt')))}.txt"
    marker.write_text("moved\n")
    git(work, "add", marker.name)
    git(work, "commit", "-m", f"{member} moved")
    if push:
        git(work, "push", "origin", "main")
    return git(work, "rev-parse", "HEAD").stdout.strip()


def test_the_members_gitlink_is_its_own_commit_and_the_sidecar_holds_the_rest(tmp_path):
    root = _full_store(tmp_path)

    assert _gitlink(root, "alpha") == _member_head(root, "alpha"), "the gitlink is the member's own commit"
    assert git(root, "ls-tree", "HEAD", "--", "alpha").stdout.split()[0] == "160000"
    names = git(root, "ls-tree", "--name-only", "HEAD:.grip/fields/alpha").stdout.split()
    assert names == ["kind", "schema"], "a covered pin: no layer, no records, and no pin (the gitlink says it)"


def test_the_grip_commit_carries_the_records_in_its_own_tree(tmp_path):
    """The field-commits were extra parents; the sidecar needs none. The first grip commit is a root commit."""
    root = _full_store(tmp_path)

    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[1:]

    assert parents == [], parents
    assert git(root, "cat-file", "blob", "HEAD:.grip/fields/beta/schema").stdout == mft.PACKAGE


def test_the_history_lists_grip_commits_only(tmp_path):
    root = _full_store(tmp_path)
    _advance(root, "beta", push=True)
    assert gr2(root, "commit", "-m", "second").returncode == 0

    chain = git(root, "log", "--format=%H").stdout.split()
    subjects = git(root, "log", "--format=%s").stdout.splitlines()

    assert subjects == ["second", "first"], subjects
    assert all(git(root, "cat-file", "-e", f"{c}:grip.toml", check=False).returncode == 0 for c in chain), chain
    assert not {_gitlink(root, "beta"), _gitlink(root, "beta", "HEAD~1")} & set(chain), "a member commit is in history"


def test_a_plain_clone_carries_the_sidecar_and_projects_nothing(tmp_path):
    root = _full_store(tmp_path)

    git(tmp_path, "clone", str(root), "plain")
    plain = tmp_path / "plain"

    assert git(plain, "cat-file", "-e", "HEAD:.grip/fields/alpha/schema", check=False).returncode == 0
    assert git(plain, "ls-tree", "HEAD", "--", "alpha").stdout.split()[2] == _member_head(root, "alpha")
    assert not (plain / ".gitmodules").exists(), "full mode never projects a .gitmodules"
    assert (plain / "alpha").is_dir() and list((plain / "alpha").iterdir()) == []


def test_the_record_reads_back_whole(tmp_path):
    from gr2.python_cli import grip_cli

    root = _full_store(tmp_path)
    member = next(m for m in grip_cli._native_members(root, "HEAD") if m["name"] == "alpha")

    record = grip_cli._field_record(root, member)

    # the url is grip.toml's (the sidecar does not repeat it); covered_by is derived from the member's upstream
    assert record == dict(schema=mft.PACKAGE, kind=mft.KIND_REPO, pin=_member_head(root, "alpha"), url="",
                          ref="main", covered_by="refs/heads/main", layers=[], records=[], ref_defaulted=False,
                          covered_test=mft.COVER_REACHABLE)


def test_an_unchanged_pin_keeps_the_same_field_commit(tmp_path):
    root = _full_store(tmp_path)
    before = _gitlink(root, "alpha")
    _advance(root, "beta", push=True)
    assert gr2(root, "commit", "-m", "only beta moved").returncode == 0

    assert _gitlink(root, "alpha") == before


def test_materialize_in_a_fresh_clone_checks_out_each_pin_from_its_url_and_covering_ref(tmp_path):
    root = _full_store(tmp_path)
    pins = {n: _member_head(root, n) for n in ("alpha", "beta")}
    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)

    done = gr2(fresh, "materialize")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    for name, pin in pins.items():
        assert git(fresh / name, "rev-parse", "HEAD").stdout.strip() == pin


def test_compat_mode_is_unchanged(tmp_path):
    """No `mode` key: the entry is the member commit itself and a .gitmodules is written, as before."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0

    assert _gitlink(root, "alpha") == _member_head(root, "alpha")
    assert (root / ".gitmodules").exists()


# ── readers that take a tree entry for a pin must read `mode` first ──────────────────────────────────────────────────


def test_a_selected_workspace_commit_agrees_with_the_field_record_not_the_raw_entry(tmp_path):
    """In full mode the entry is a field-commit; `_native_members` at a revision compares the pin the record holds."""
    from gr2.python_cli import grip_cli

    root = _full_store(tmp_path)

    members = grip_cli._native_members(root, "HEAD")

    assert sorted(m["name"] for m in members) == ["alpha", "beta"]
    assert {m["name"]: m["pin"] for m in members} == {n: _member_head(root, n) for n in ("alpha", "beta")}


def test_a_plain_git_commit_is_refused_by_the_hook_by_name_in_full_mode_with_no_shadow(tmp_path):
    # Section 9 narrowed the hook: with a shadow, a hand commit that edits no pin passes (test_store_shadow_fence.py).
    # With no shadow there is nothing to fence against, so the hook still refuses every hand commit by name.
    root = _full_store(tmp_path)
    git(root, "update-ref", "-d", "refs/dev.synapt.grip/__branches__/main/SHADOW")

    refused = git(root, "commit", "--allow-empty", "-m", "plain", check=False)

    assert refused.returncode != 0
    assert "full_mode_hook" in f"{refused.stdout}{refused.stderr}", (refused.stdout, refused.stderr)


@pytest.mark.parametrize("reader", ["_scan_existing_repos", "_require_superproject", "_superproject_report"])
def test_the_superproject_readers_refuse_a_full_mode_root_by_name(tmp_path, reader):
    from gr2.python_cli import app

    root = _full_store(tmp_path)

    with pytest.raises(SystemExit) as stopped:
        getattr(app, reader)(root)

    assert "full_mode_unsupported" in str(stopped.value), stopped.value


def test_a_compat_root_still_reads_its_gitlinks_as_pins(tmp_path):
    from gr2.python_cli import app

    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0

    assert sorted(app._root_gitlink_pins(root)) == ["alpha", "beta"]


def test_migrate_already_refuses_a_root_that_has_a_grip_toml(tmp_path):
    """`store migrate` writes member pins as gitlinks, which would overwrite a field-commit; it never reaches that."""
    root = _full_store(tmp_path)

    refused = gr2(root, "migrate", check=False)

    assert refused.returncode != 0


def test_a_mode_that_is_neither_compat_nor_full_is_refused_by_name(tmp_path):
    root = _full_store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8").replace('mode = "full"\n', 'mode = "fulll"\n', 1)
    (root / "grip.toml").write_text(text, encoding="utf-8")

    refused = gr2(root, "commit", "-m", "typo", check=False)

    assert refused.returncode == 4
    assert 'mode must be "compat" or "full"' in f"{refused.stdout}{refused.stderr}"


def test_materialize_fetches_the_covering_ref_when_the_pin_is_on_no_default_branch(tmp_path):
    """The pin is covered by `origin/side` only: a plain clone does not hold it, so materialize must fetch the ref the
    record names (never ask for a bare sha) before it checks the pin out."""
    root = _full_store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    assert text.count('upstream = "origin/main"') == 2
    (root / "grip.toml").write_text(text.replace('upstream = "origin/main"', 'upstream = "origin/side"', 1), encoding="utf-8")
    pin = _advance(root, "alpha", push=False)
    git(root / "alpha", "push", "origin", "HEAD:refs/heads/side")
    done = gr2(root, "commit", "-m", "alpha covered by side only")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    # A plain clone fetches every branch, so `origin/side` would arrive without the fetch and this row could not fail.
    # A single-branch checkout of main leaves the covering ref as the only way the pin can arrive.
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    git(fresh, "clone", "--no-local", "--single-branch", "--branch", "main", url, "alpha")
    assert git(fresh / "alpha", "cat-file", "-t", pin, check=False).returncode != 0

    made = gr2(fresh, "materialize")

    assert made.returncode == 0, f"{made.stdout}\n{made.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin


def test_a_shallow_clone_reads_the_records_and_materializes(tmp_path):
    """The field-commits were PARENTS and a depth-1 clone lacked them (a named refusal). The sidecar is in the grip
    commit's own tree, so a depth-1 clone has every record and materializes each pin."""
    root = _full_store(tmp_path)
    _advance(root, "alpha", push=True)
    assert gr2(root, "commit", "-m", "alpha moved").returncode == 0
    git(tmp_path, "clone", "--depth", "1", f"file://{root}", "shallow")

    made = gr2(tmp_path / "shallow", "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode == 0 and "Traceback" not in out, out
    assert git(tmp_path / "shallow" / "alpha", "rev-parse", "HEAD").stdout.strip() == _member_head(root, "alpha")


@pytest.mark.parametrize("verb", ["check", "status"])
def test_check_and_status_read_the_record_pin_of_a_clean_full_mode_commit(tmp_path, verb):
    root = _full_store(tmp_path)

    done = gr2(root, verb)

    out = f"{done.stdout}\n{done.stderr}"
    assert "disagrees with pin" not in out, out
    assert done.returncode == 0, out


def test_diff_reads_the_gitlinks_of_two_grip_commits(tmp_path):
    """The empty root commit that held the first-parent place is gone (the sidecar adds no parents), so its refusal
    has no case to name; the diff of two real grip commits is what remains."""
    root = _full_store(tmp_path)
    before = _member_head(root, "alpha")
    after = _advance(root, "alpha", push=True)
    assert gr2(root, "commit", "-m", "alpha moved").returncode == 0

    real = gr2(root, "diff", "HEAD~1", "HEAD")

    assert real.returncode == 0, f"{real.stdout}\n{real.stderr}"
    assert f"alpha {before} -> {after}" in real.stdout, real.stdout


def test_a_compat_store_that_flips_to_full_drops_the_gitmodules_gr2_wrote(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "compat first").returncode == 0
    assert git(root, "cat-file", "-e", "HEAD:.gitmodules", check=False).returncode == 0
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text('mode = "full"\n' + text, encoding="utf-8")
    _advance(root, "alpha", push=True)

    flipped = gr2(root, "commit", "-m", "flip to full")

    assert flipped.returncode == 0, f"{flipped.stdout}\n{flipped.stderr}"
    assert git(root, "cat-file", "-e", "HEAD:.gitmodules", check=False).returncode != 0
    assert not (root / ".gitmodules").exists()


def _evil_root(tmp_path: Path, marker: Path, covered_by: str) -> Path:
    """A full-mode root in the field-commit shape whose alpha record names `covered_by`, carried in a root commit as any
    pusher could."""
    root = _full_store(tmp_path)
    pin = _member_head(root, "alpha")
    evil = mft.commit(root, dict(kind=mft.KIND_REPO, pin=pin, url=git(root / "alpha", "remote", "get-url", "origin").stdout.strip(),
                                 ref="main", covered_by=covered_by, covered_test=mft.COVER_REACHABLE), "evil record")
    # the shape before the sidecar: with `.grip/fields` left in place the reader takes the sidecar and never reads this
    git(root, "rm", "-r", "-q", "--cached", "--sparse", "--ignore-unmatch", ".grip/fields")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{evil},alpha")
    tree = git(root, "write-tree").stdout.strip()
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    made = git(root, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit-tree", tree,
               "-p", head, "-p", evil, "-m", "carry the evil record").stdout.strip()
    git(root, "update-ref", "HEAD", made)
    return root


def _evil_upstream_root(tmp_path: Path, branch: str) -> Path:
    """A full-mode root with a sidecar whose grip.toml names alpha's upstream branch `branch`, the field the sidecar
    reader derives covered_by from, carried in a root commit made by plain git, as any pusher could."""
    root = _full_store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    first = text.index('upstream = "origin/main"')
    text = text[:first] + f'upstream = "origin/{branch}"' + text[first + len('upstream = "origin/main"'):]
    (root / "grip.toml").write_text(text, encoding="utf-8")
    git(root, "add", "grip.toml")
    git(root, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "-q", "--no-verify", "-m",
        "carry the evil upstream")
    return root


def test_a_sidecar_whose_covering_ref_is_an_option_is_refused_and_runs_nothing(tmp_path):
    """The sidecar shape of the row below: covered_by is derived from grip.toml's upstream, so the option rides there."""
    marker = tmp_path / "PWNED"
    root = _evil_upstream_root(tmp_path, f"--upload-pack=touch {marker}; git-upload-pack")
    git(tmp_path, "clone", str(root), "victim")

    made = gr2(tmp_path / "victim", "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode != 0, out
    assert not marker.exists(), "the upstream ran as a command"
    assert "covered_by" in out and "Traceback" not in out, out


def test_legacy_a_record_whose_covering_ref_is_an_option_is_refused_and_runs_nothing(tmp_path):
    """A record whose covered_by is an option (`--upload-pack=<command>`) is refused and nothing runs: it was handed to
    `git fetch` and ran."""
    marker = tmp_path / "PWNED"
    root = _evil_root(tmp_path, marker, f"--upload-pack=touch {marker}; git-upload-pack")
    git(tmp_path, "clone", str(root), "victim")

    made = gr2(tmp_path / "victim", "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode != 0, out
    assert not marker.exists(), "the record's covered_by ran as a command"
    assert "covered_by" in out, out


@pytest.mark.parametrize("field,value", [
    ("covered_by", "refs/tags/v1"),
    ("covered_by", "refs/heads/a..b"),
    ("covered_by", "-x"),
    ("pin", "not-hex"),
    ("pin", "abc123"),
    ("ref", "-x"),
    ("url", "--upload-pack=x"),
    ("url", "ext::sh -c id"),
    ("url", "https://user:secret@example.invalid/r.git"),
    ("url", "https://example.invalid/r.git extra"),
])
def test_a_record_field_that_git_would_act_on_as_an_option_is_refused_at_the_read(tmp_path, field, value):
    git(tmp_path, "init", "-q", "-b", "main")
    good = dict(kind=mft.KIND_REPO, pin="a" * 40, url="https://example.invalid/r.git", ref="main",
                covered_by="refs/heads/main", covered_test=mft.COVER_REACHABLE)
    assert mft.read(tmp_path, f"{mft.write(tmp_path, good)}")["pin"] == "a" * 40
    tree = mft.write(tmp_path, {**good, field: value})

    with pytest.raises(mft.FieldTreeError, match=field):
        mft.read(tmp_path, tree)

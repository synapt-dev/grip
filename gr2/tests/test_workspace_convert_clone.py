"""Step 3 of the worktree refactor: ``convert_worktree_to_clone``
converts a linked worktree into an own clone, in place, replacing the
by-hand proof (config main, worktree-refactor design note) with real code.

The witness: ``.git`` lstat before (a worktree pointer file) and after (a
real directory) at the SAME path, plus the canonical repo's own
``worktree list`` showing the link genuinely gone -- not merely hidden.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import repo_maintenance_prototype as repo_proto


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)
    (path / "README.md").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def test_convert_worktree_to_clone_replaces_the_pointer_file_with_a_real_directory(tmp_path):
    canonical = tmp_path / "canonical"
    _init_repo(canonical)

    linked = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "convert-branch", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
        text=True,
    )

    before_status = repo_proto.inspect_repo(linked)
    assert before_status.linked_worktree is True  # precondition the fix needs

    receipt = repo_proto.convert_worktree_to_clone(linked)

    # The witness: .git at the SAME path, before a pointer file, now a real directory.
    assert receipt["before_git_lstat"]["is_regular_file"] is True
    assert receipt["before_git_lstat"]["is_dir"] is False
    assert receipt["after_git_lstat"]["is_dir"] is True
    assert receipt["after_git_lstat"]["is_regular_file"] is False

    # The canonical repo's OWN worktree list no longer carries the link -- not
    # merely hidden from some other view, actually removed from the registry
    # `git worktree list` reads.
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=canonical,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(linked) not in list_result.stdout

    # The converted path is a real, independent, functioning clone.
    after_status = repo_proto.inspect_repo(linked)
    assert after_status.linked_worktree is False
    assert after_status.git_dir_is_symlink is False
    assert after_status.is_git_repo is True
    assert after_status.branch == "convert-branch"


def test_convert_worktree_to_clone_refuses_a_dirty_tree(tmp_path):
    canonical = tmp_path / "canonical"
    _init_repo(canonical)

    linked = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "dirty-branch", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
        text=True,
    )
    (linked / "untracked.txt").write_text("uncommitted\n")

    with pytest.raises(repo_proto.ConvertCloneError, match="dirty"):
        repo_proto.convert_worktree_to_clone(linked)

    # Refused BEFORE any mutation -- the worktree link is untouched.
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=canonical,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(linked) in list_result.stdout


def test_convert_worktree_to_clone_refuses_a_symlinked_git_dir(tmp_path):
    # Deliberately NOT named anything containing "symlink" -- a fixture path
    # that does is how the original version of this test passed for the
    # wrong reason: `match="symlink"` matched the FIXTURE'S OWN DIRECTORY
    # NAME embedded in every ConvertCloneError message, not the symlink
    # class of refusal. Reproduced empirically: forcing
    # `is_git_dir_symlink` to always return False still raised (correctly,
    # via the generic "not a linked worktree" branch) and the old assertion
    # still matched, because the fixture was named "symlinked".
    real = tmp_path / "real"
    _init_repo(real)

    desk_repo = tmp_path / "desk-repo"
    desk_repo.mkdir()
    (desk_repo / ".git").symlink_to(real / ".git")

    # Match a phrase that exists ONLY in the symlink-specific message, never
    # in the generic "not a linked worktree" message the same fixture would
    # raise if the symlink-specific check were disabled -- that distinction
    # is the actual "message class" this test exists to prove.
    with pytest.raises(repo_proto.ConvertCloneError, match="is a symlink into another clone"):
        repo_proto.convert_worktree_to_clone(desk_repo)


def test_convert_worktree_to_clone_refuses_an_own_clone(tmp_path):
    origin = tmp_path / "origin"
    _init_repo(origin)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)

    with pytest.raises(repo_proto.ConvertCloneError, match="not a linked worktree"):
        repo_proto.convert_worktree_to_clone(clone)


def test_convert_refuses_when_the_branch_moves_between_head_read_and_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuine concurrent-write race: the worktree's own branch ref
    # advances (via `update-ref` on the CANONICAL repo's shared ref store,
    # never touching the worktree's own working directory -- what makes
    # this concurrent rather than merely "the tree changed under us") in
    # the exact window between convert_worktree_to_clone reading head_sha
    # and checking the staging clone out onto that branch. The staging
    # isolation guard exists to refuse in exactly this window; without it,
    # a converted clone would silently land on a different commit than the
    # one the caller observed and approved.
    canonical = tmp_path / "canonical"
    _init_repo(canonical)

    linked = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "raced-branch", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
        text=True,
    )

    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=linked, check=True, capture_output=True, text=True
    ).stdout.strip()
    real_run_git = repo_proto.run_git

    def racing_run_git(path: Path, *args: str):
        result = real_run_git(path, *args)
        if path == linked and args == ("rev-parse", "HEAD"):
            new_tree = subprocess.run(
                [
                    "git",
                    "commit-tree",
                    "-p",
                    original_head,
                    "-m",
                    "raced commit",
                    f"{original_head}^{{tree}}",
                ],
                cwd=canonical,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", "refs/heads/raced-branch", new_tree],
                cwd=canonical,
                check=True,
                capture_output=True,
                text=True,
            )
        return result

    monkeypatch.setattr(repo_proto, "run_git", racing_run_git)

    with pytest.raises(repo_proto.ConvertCloneError, match="HEAD"):
        repo_proto.convert_worktree_to_clone(linked)

    # The guard must fire and refuse BEFORE `git worktree remove` runs --
    # the original worktree's registration and its `.git` pointer file are
    # both untouched.
    list_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=canonical,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(linked) in list_result.stdout
    st = (linked / ".git").lstat()
    assert stat.S_ISREG(st.st_mode)


# ---------------------------------------------------------------------------
# The conversion must not trade a .git coupling for an origin
# coupling. `git clone file://<owner>` sets origin to the OWNER'S DIRECTORY,
# so a desk whose origin was the real remote comes out pointing at a peer's
# working copy: decoupled at .git, still coupled through origin, and it breaks
# the day that directory moves.
# ---------------------------------------------------------------------------


def _origin_url(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_convert_preserves_the_originals_origin_instead_of_the_owner_directory(tmp_path):
    canonical = tmp_path / "canonical"
    _init_repo(canonical)
    remote = tmp_path / "real-remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(canonical), str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=canonical, check=True)

    linked = tmp_path / "desk"
    subprocess.run(
        ["git", "worktree", "add", "-b", "deskbr", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
    )
    # precondition: the worktree already resolves a CORRECT origin
    assert _origin_url(linked) == str(remote)

    repo_proto.convert_worktree_to_clone(linked)

    after = _origin_url(linked)
    assert after is not None, "the conversion dropped origin entirely"
    assert str(canonical) not in after, (
        f"origin points at the owner's directory ({after}) -- a .git coupling "
        "was traded for an origin coupling"
    )
    assert after == str(remote)


def test_convert_does_not_invent_an_origin_when_the_original_had_none(tmp_path):
    """A repo with no remote is legitimate. The conversion must not manufacture
    one pointing at the owner -- inventing a coupling is worse than none."""
    canonical = tmp_path / "canonical"
    _init_repo(canonical)

    linked = tmp_path / "desk"
    subprocess.run(
        ["git", "worktree", "add", "-b", "deskbr", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
    )
    assert _origin_url(linked) is None  # precondition

    repo_proto.convert_worktree_to_clone(linked)

    assert _origin_url(linked) is None, (
        "the conversion invented an origin where the original had none"
    )


# ---------------------------------------------------------------------------
# The original is destroyed before an unguarded move, so any
# failure in that window leaves the desk path EMPTY with the replacement
# stranded at a path no message names. Found and fixed in a
# hand-built equivalent; absent here.
# ---------------------------------------------------------------------------


def _make_linked(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    _init_repo(canonical)
    linked = tmp_path / "desk"
    subprocess.run(
        ["git", "worktree", "add", "-b", "deskbr", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
    )
    return canonical, linked


def test_a_failed_swap_leaves_the_original_at_its_own_path(tmp_path, monkeypatch):
    canonical, linked = _make_linked(tmp_path)
    (linked / "desk-only.txt").write_text("the only copy\n")
    subprocess.run(["git", "add", "-A"], cwd=linked, check=True)
    subprocess.run(["git", "commit", "-qm", "desk work"], cwd=linked, check=True)

    real_move = repo_proto.shutil.move

    def explode(src, dst, *args, **kwargs):
        # fail only the swap INTO the desk path, not staging cleanup
        if str(dst) == str(linked):
            raise OSError("injected: the swap failed here")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(repo_proto.shutil, "move", explode)

    with pytest.raises(Exception):
        repo_proto.convert_worktree_to_clone(linked)

    assert linked.exists(), "the desk path is gone after a failed swap"
    assert (linked / "desk-only.txt").read_text() == "the only copy\n"
    assert (linked / ".git").exists()


def test_a_failed_swap_does_not_nest_the_replacement_inside_an_occupant(tmp_path, monkeypatch):
    """`mv src dst` with dst present NESTS src inside it (measured).
    If anything recreates the path during the window, refuse and keep both
    copies findable rather than producing a non-git directory."""
    canonical, linked = _make_linked(tmp_path)

    real_rename = repo_proto.os.rename

    def recreate_after_aside(src, dst, *args, **kwargs):
        result = real_rename(src, dst, *args, **kwargs)
        if str(src) == str(linked):
            # a peer / materializer recreates the path inside the window
            Path(linked).mkdir(parents=True, exist_ok=True)
            (Path(linked) / "interloper.txt").write_text("not mine\n")
        return result

    monkeypatch.setattr(repo_proto.os, "rename", recreate_after_aside)

    with pytest.raises(Exception):
        repo_proto.convert_worktree_to_clone(linked)

    assert linked.exists()
    # the original tree is recoverable, and the occupant was not deleted
    survivors = list(tmp_path.glob("*interloper*")) + list(tmp_path.glob("desk*"))
    assert survivors, "neither the original nor the occupant is findable"


def test_restore_moves_an_occupant_aside_rather_than_deleting_it(tmp_path):
    """The RECOVERY path must not carry the defect the forward path was fixed
    for. A hand-built equivalent did an unguarded delete here and then printed
    that nothing was lost -- and a recovery path runs, by definition, once
    something has already gone wrong."""
    target = tmp_path / "target"
    aside = tmp_path / "aside"
    aside.mkdir()
    (aside / "original.txt").write_text("the original\n")
    target.mkdir()
    (target / "occupant.txt").write_text("someone else's bytes\n")

    ok = repo_proto.restore_original(target, aside)

    assert ok is True
    assert (target / "original.txt").read_text() == "the original\n"
    kept = list(tmp_path.glob("*interloper*"))
    assert kept, "the occupant was deleted instead of moved aside and named"
    assert (kept[0] / "occupant.txt").read_text() == "someone else's bytes\n"


# ---------------------------------------------------------------------------
# A worktree's commits live in the OWNER's object store, which
# has been acting as an accidental backup nobody designed. Conversion removes
# that fallback, so the verb says so while saying it is still cheap.
# ---------------------------------------------------------------------------


def test_receipt_counts_commits_that_exist_on_no_remote(tmp_path):
    canonical = tmp_path / "canonical"
    _init_repo(canonical)
    remote = tmp_path / "rem.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(canonical), str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=canonical, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=canonical, check=True)

    linked = tmp_path / "desk"
    subprocess.run(
        ["git", "worktree", "add", "-b", "deskbr", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
    )
    (linked / "desk.txt").write_text("only copy\n")
    subprocess.run(["git", "add", "-A"], cwd=linked, check=True)
    subprocess.run(["git", "commit", "-qm", "desk only"], cwd=linked, check=True)

    receipt = repo_proto.convert_worktree_to_clone(linked)
    assert receipt["unpushed_commits"] == 1


def test_receipt_reports_zero_when_the_tip_is_already_on_a_remote(tmp_path):
    """The control for the case above. `git log --not --remotes` with no
    POSITIVE ref yields nothing at all, so the count could never fire and the
    subject case alone would pass against a check that cannot fail."""
    canonical = tmp_path / "canonical"
    _init_repo(canonical)
    remote = tmp_path / "rem.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(canonical), str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=canonical, check=True)

    linked = tmp_path / "desk"
    subprocess.run(
        ["git", "worktree", "add", "-b", "deskbr", str(linked)],
        cwd=canonical,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "fetch", "-q", str(canonical), "deskbr:deskbr"], cwd=remote, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=canonical, check=True)

    receipt = repo_proto.convert_worktree_to_clone(linked)
    assert receipt["unpushed_commits"] == 0


def test_work_written_during_the_clone_window_is_not_silently_destroyed(
    tmp_path, monkeypatch
):
    """The dirty gate runs BEFORE the clone, and the clone is the long part.
    Work written into the tree during that window is invisible to that gate,
    captured by the rename-aside, and deleted by the success-path cleanup --
    with success reported. Verify-then-act over a mutating world, with the
    user as the victim on the happy path."""
    canonical, linked = _make_linked(tmp_path)

    real_run = repo_proto.run_git

    def write_during_clone(cwd, *args, **kwargs):
        result = real_run(cwd, *args, **kwargs)
        # the moment the staging checkout happens, the user saves a file
        if args[:1] == ("checkout",) and "staging" in str(cwd):
            (linked / "written-during-window.txt").write_text("my work\n")
        return result

    monkeypatch.setattr(repo_proto, "run_git", write_during_clone)

    with pytest.raises(Exception):
        repo_proto.convert_worktree_to_clone(linked)

    assert (linked / "written-during-window.txt").read_text() == "my work\n", (
        "work written during the clone window was destroyed"
    )

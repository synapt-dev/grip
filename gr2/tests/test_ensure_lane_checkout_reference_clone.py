"""Contract (grip#807, 2026-08-31): ``ensure_lane_checkout`` materializes
every lane repo as an INDEPENDENT reference clone, never a ``git worktree add``.

Two agents (or two lanes) must be able to check out the same branch without
sharing refs, HEAD, reflogs, index, locks, config, or working-tree state. Git
worktrees cannot provide that; the current implementation uses one, so every
isolation assertion below is RED before the fix.

The witnesses travel the real function (`ensure_lane_checkout`), and read the
resulting on-disk clone, not a mock. The source repo is given a canonical
``origin`` URL (a local bare repo) so the URL-derivation path is exercised; the
unpushed-seed cases put a commit only in the source checkout, never on origin.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from gr2.python_cli import clone_exec
from gr2.python_cli.gitops import ensure_lane_checkout, git


def _run(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_source(root: Path, name: str) -> tuple[Path, Path]:
    """A bare 'origin' repo plus a source checkout with one pushed commit on
    'main'. Returns (source_checkout, origin_url_path)."""
    origin = root / f"{name}.git"
    _run(root, "init", "--bare", "-b", "main", str(origin))
    source = root / name
    _run(root, "clone", "--quiet", str(origin), str(source))
    _run(source, "config", "user.email", "t@t")
    _run(source, "config", "user.name", "t")
    (source / "README.md").write_text("seed\n")
    _run(source, "add", ".")
    _run(source, "commit", "-q", "-m", "initial")
    _run(source, "push", "-q", "origin", "main")
    return source, origin


def _seed_unpushed_branch(source: Path, branch: str) -> str:
    """Create a branch with a commit that exists ONLY in the source checkout
    (never pushed to origin). Returns the seed sha. Leaves source on main."""
    _run(source, "checkout", "-q", "-b", branch)
    (source / f"{branch}.txt").write_text("lane work\n")
    _run(source, "add", ".")
    _run(source, "commit", "-q", "-m", f"work on {branch}")
    sha = _run(source, "rev-parse", "HEAD")
    _run(source, "checkout", "-q", "main")
    return sha


def _git_common_dir(clone: Path) -> Path:
    out = _run(clone, "rev-parse", "--git-common-dir")
    return Path(clone / out).resolve()


def test_lane_clone_has_its_own_git_directory(tmp_path):
    """The core invariant: the lane is a normal clone. ``.git`` is a real
    directory, its common-dir resolves inside it, and it hosts no worktrees.
    A worktree checkout fails all three (``.git`` is a pointer file whose
    common-dir is the source's)."""
    source, _ = _init_source(tmp_path, "repo")
    lane = tmp_path / "lanes" / "a" / "repo"

    first = ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="main")
    assert first is True
    git_dir = lane / ".git"
    assert git_dir.is_dir(), ".git must be a directory, not a worktree pointer file"
    assert _git_common_dir(lane) == git_dir.resolve()
    assert not (git_dir / "worktrees").exists()


def test_two_lanes_same_branch_are_independent(tmp_path):
    """Fruit 1 + 3: two lanes materialize the same branch and each has its own
    .git; mutating refs/config/working-tree in one changes neither the source
    nor the sibling."""
    source, _ = _init_source(tmp_path, "repo")
    lane_a = tmp_path / "lanes" / "a" / "repo"
    lane_b = tmp_path / "lanes" / "b" / "repo"
    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane_a, branch="main")
    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane_b, branch="main")

    assert (lane_a / ".git").is_dir() and (lane_b / ".git").is_dir()
    assert _git_common_dir(lane_a) != _git_common_dir(lane_b)

    # Mutate a config value + a new ref + a working-tree file in lane_a only.
    _run(lane_a, "config", "lane.marker", "aaa")
    _run(lane_a, "branch", "lane-only-ref")
    (lane_a / "dirty.txt").write_text("uncommitted\n")

    assert git(lane_b, "config", "lane.marker").returncode != 0
    assert git(lane_b, "show-ref", "--verify", "refs/heads/lane-only-ref").returncode != 0
    assert not (lane_b / "dirty.txt").exists()
    assert git(source, "show-ref", "--verify", "refs/heads/lane-only-ref").returncode != 0
    assert not (source / "dirty.txt").exists()


def test_unpushed_branch_lands_at_source_commit(tmp_path):
    """Fruit 2 + behavior 2: a branch that exists only in the source checkout
    seeds the lane at that exact commit, and the source is left neither a remote
    nor an alternate of the lane."""
    source, _ = _init_source(tmp_path, "repo")
    seed = _seed_unpushed_branch(source, "feature")
    lane = tmp_path / "lanes" / "a" / "repo"

    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="feature")

    assert _run(lane, "rev-parse", "HEAD") == seed
    assert _run(lane, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    # No remote points at the source CHECKOUT: exact per-remote URL match, since
    # the canonical origin (repo.git) legitimately shares a path prefix with the
    # source checkout (repo). origin must be the canonical URL, nothing else.
    remote_names = _run(lane, "remote").split()
    remote_urls = {n: _run(lane, "remote", "get-url", n) for n in remote_names}
    assert str(source) not in remote_urls.values()
    assert remote_names == ["origin"]
    assert remote_urls["origin"] == _run(source, "remote", "get-url", "origin")
    # The source is not an alternate object store of the lane.
    alt = lane / ".git" / "objects" / "info" / "alternates"
    if alt.exists():
        assert str(source) not in alt.read_text()


def test_explicit_seed_is_not_reinterpreted_as_a_branch_or_source_head(tmp_path):
    """A review pin is a bound object, while the lane branch is merely local.

    The source stays on main so a regression that falls back to source HEAD lands
    at a different commit. The local branch remains a stable, safe lane name.
    """
    source, _ = _init_source(tmp_path, "repo")
    review_head = _seed_unpushed_branch(source, "review-alpha")
    source_head = _run(source, "rev-parse", "HEAD")
    assert review_head != source_head
    lane = tmp_path / "lanes" / "a" / "repo"

    ensure_lane_checkout(
        source_repo_root=source,
        target_repo_root=lane,
        branch="grip-review/open",
        seed_commit=review_head,
    )

    assert _run(lane, "rev-parse", "HEAD") == review_head
    assert _run(lane, "rev-parse", "--abbrev-ref", "HEAD") == "grip-review/open"


def test_absent_branch_is_created_from_source_head(tmp_path):
    """Behavior 2: no such branch anywhere -> seed from source HEAD and create
    it. The lane's new branch sits at the source's current HEAD commit."""
    source, _ = _init_source(tmp_path, "repo")
    head = _run(source, "rev-parse", "HEAD")
    lane = tmp_path / "lanes" / "a" / "repo"

    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="brand-new")

    assert _run(lane, "rev-parse", "--abbrev-ref", "HEAD") == "brand-new"
    assert _run(lane, "rev-parse", "HEAD") == head
    # And it is an independent clone, not a worktree sharing the source's refs.
    assert (lane / ".git").is_dir()
    assert _git_common_dir(lane) == (lane / ".git").resolve()
    # The created branch must NOT appear in the source's ref namespace.
    assert git(source, "show-ref", "--verify", "refs/heads/brand-new").returncode != 0


def test_a_linked_worktree_parked_at_dest_is_refused(tmp_path):
    """Fruit 4: a real linked worktree already sitting at the destination is
    refused, not silently accepted as a valid lane checkout."""
    source, _ = _init_source(tmp_path, "repo")
    lane = tmp_path / "lanes" / "a" / "repo"
    lane.parent.mkdir(parents=True, exist_ok=True)
    # Park a genuine linked worktree of the SOURCE at the destination.
    _run(source, "worktree", "add", "-b", "wt", str(lane), "HEAD")
    assert (lane / ".git").is_file()  # worktree pointer, as the old impl produced

    with pytest.raises((SystemExit, Exception)):
        ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="main")


def test_dirty_valid_lane_survives_repeat_materialization(tmp_path):
    """Fruit 5 + behavior 6: a second materialization over a healthy lane leaves
    it byte-for-byte, including uncommitted files. It never resets or stashes."""
    source, _ = _init_source(tmp_path, "repo")
    lane = tmp_path / "lanes" / "a" / "repo"
    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="main")

    (lane / "wip.txt").write_text("do not lose me\n")
    local_sha_before = _run(lane, "rev-parse", "HEAD")

    second = ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="main")
    assert second is False
    assert (lane / "wip.txt").read_text() == "do not lose me\n"
    assert _run(lane, "rev-parse", "HEAD") == local_sha_before


def test_concurrent_creators_produce_one_valid_clone_no_residue(tmp_path):
    """Fruit 6 + behavior 5: two concurrent creators of the same destination
    yield one valid clone, no overwrite, and no staging residue beside it."""
    source, _ = _init_source(tmp_path, "repo")
    lane = tmp_path / "lanes" / "a" / "repo"

    def create() -> bool:
        return ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="main")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        results = [f.result() for f in [ex.submit(create), ex.submit(create)]]

    assert sorted(results) == [False, True]  # exactly one first-materialize
    assert (lane / ".git").is_dir()
    assert _git_common_dir(lane) == (lane / ".git").resolve()
    # No staging directory left beside the destination.
    residue = [p for p in lane.parent.iterdir() if p != lane]
    assert residue == [], f"staging residue left behind: {residue}"


# --- grip#807 v2: findings from the r1 boundary probes -----------------------


def test_relative_origin_url_is_resolved_against_source(tmp_path):
    """Sentinel v1: a valid RELATIVE filesystem origin resolves in the source
    repo, but the clone runs from a staging cwd where the same relative string
    points elsewhere. The lane must resolve it against the source, not the caller
    cwd. Before the fix the clone from staging fails; after, the lane lands at the
    source commit."""
    source, origin = _init_source(tmp_path, "repo")
    # Rewrite origin to a path relative to the source checkout. git resolves it
    # against the source (proven by a fetch that succeeds from there).
    rel = os.path.relpath(origin, source)
    _run(source, "remote", "set-url", "origin", rel)
    _run(source, "fetch", "origin")  # sanity: valid FROM the source repo

    lane = tmp_path / "lanes" / "a" / "repo"
    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="main")

    assert (lane / ".git").is_dir()
    assert _run(lane, "rev-parse", "HEAD") == _run(source, "rev-parse", "HEAD")


def test_publish_refuses_a_pre_existing_dest_it_did_not_create(tmp_path):
    """Kills the "drop the under-lock absence check" mutant. os.rename and
    os.replace are equivalent for directories on POSIX, so the guarantee is NOT
    the rename verb -- it is the absence check taken while the lock is held. With
    a directory already at dest, a correct publish routes to reuse (which refuses
    a non-clone directory); dropping the check lets the rename replace it. Uses an
    empty dir, the strongest form: even that must not be moved onto."""
    source, origin = _init_source(tmp_path, "repo")
    staging = tmp_path / "staging"
    _run(tmp_path, "clone", "--quiet", str(origin), str(staging))
    dest = tmp_path / "lanes" / "a" / "repo"
    dest.mkdir(parents=True)  # a directory already sitting at dest
    repo_url = _run(source, "remote", "get-url", "origin")

    with pytest.raises(clone_exec.CloneExecutionError):
        clone_exec._publish_lane_atomically(
            staging,
            dest,
            workspace_root=tmp_path,
            repo_url=repo_url,
            reference_base=None,
            expected_branch="main",
        )
    # The pre-existing dest was NOT overwritten by the staged clone.
    assert not (dest / ".git").exists()


def test_publish_waits_for_a_held_lock_then_reuses_the_winner(tmp_path):
    """Kills the "drop the per-destination lock" mutant, deterministically. With
    the lock held by a (simulated) concurrent winner and dest still absent, a
    correct publisher must WAIT -- not publish. A mutant that ignores the lock
    would rename its staging straight onto the absent dest and finish immediately.
    The 0.3s observation window is unambiguous: a correct publisher polls forever
    until dest appears; a lock-less one publishes at once."""
    source, origin = _init_source(tmp_path, "repo")
    dest = tmp_path / "lanes" / "a" / "repo"
    dest.parent.mkdir(parents=True)
    lockfile = dest.parent / f".{dest.name}.publish.lock"
    lockfile.touch()  # a concurrent winner holds the publish lock

    staging = tmp_path / "staging"
    _run(tmp_path, "clone", "--quiet", str(origin), str(staging))
    repo_url = _run(source, "remote", "get-url", "origin")

    result: dict[str, object] = {}

    def publish() -> None:
        result["ret"] = clone_exec._publish_lane_atomically(
            staging,
            dest,
            workspace_root=tmp_path,
            repo_url=repo_url,
            reference_base=None,
            expected_branch="main",
        )

    t = threading.Thread(target=publish)
    t.start()
    time.sleep(0.3)
    # A correct publisher is still waiting; it has NOT published while dest absent.
    assert t.is_alive(), "publisher did not wait for the held lock (no-lock mutant)"
    assert not (dest / ".git").exists(), "publisher moved onto dest while lock held"

    # The winner publishes a valid clone at dest and releases the lock.
    winner = tmp_path / "winner"
    _run(tmp_path, "clone", "--quiet", str(origin), str(winner))
    (winner / "winner-marker.txt").write_text("winner\n")
    os.rename(str(winner), str(dest))
    lockfile.unlink(missing_ok=True)

    t.join(timeout=15)
    assert not t.is_alive()
    assert result["ret"] is False  # loser reused, did not publish
    assert (dest / "winner-marker.txt").exists()  # winner left untouched
    assert not staging.exists()  # loser discarded only its own staging


def test_seed_binds_selected_commit_across_a_concurrent_ref_move(tmp_path, monkeypatch):
    """Kills the "fetch the ref / check out FETCH_HEAD / no bind" mutants,
    deterministically. The seed must bind the commit resolved at selection even if
    the source branch moves before the fetch. The move is injected exactly in that
    window: right after the seed rev-parse in the source returns, the source branch
    is advanced once. A correct impl fetches and checks out the BOUND SHA (the
    selected commit); an impl that fetched the ref and checked out FETCH_HEAD would
    land on the moved commit."""
    source, _ = _init_source(tmp_path, "repo")
    c1 = _seed_unpushed_branch(source, "feature")  # the commit selection resolves
    lane = tmp_path / "lanes" / "a" / "repo"

    real_git = clone_exec.gitops.git
    state = {"advanced": False}

    def spy(repo, *args, **kwargs):
        res = real_git(repo, *args, **kwargs)
        # Fire once, immediately after the seed is resolved in the source.
        if not state["advanced"] and args and args[0] == "rev-parse" and str(repo) == str(source):
            state["advanced"] = True
            _run(source, "checkout", "-q", "feature")
            (source / "moved.txt").write_text("moved after selection\n")
            _run(source, "add", ".")
            _run(source, "commit", "-q", "-m", "advance feature past the seed")
            _run(source, "checkout", "-q", "main")
        return res

    monkeypatch.setattr(clone_exec.gitops, "git", spy)

    ensure_lane_checkout(source_repo_root=source, target_repo_root=lane, branch="feature")

    c2 = _run(source, "rev-parse", "feature")
    assert c2 != c1, "the injected move did not fire; the test proves nothing"
    assert state["advanced"] is True
    # The lane bound the SELECTED commit, not wherever the ref moved to.
    assert _run(lane, "rev-parse", "HEAD") == c1

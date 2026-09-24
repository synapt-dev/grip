"""The read-through follow-on: every site that asked ``is_git_repo`` about a
declared member path got the ENCLOSING repository's answer.

Measured 2026-09-24 on dev ``f2c8a3f6``: four production sites fail on plain
directories at declared repo paths, each in a different direction. The helper
answers a QUESTION (repo root / empty placeholder / neither)
and each site decides what to do with the answer.

WHY THE PLACEHOLDER CASE IS A WITNESS AND NOT A DETAIL: a plain ``git clone``
of a superproject leaves every member path an EMPTY directory until
``submodule update --init``. That is the ordinary state of a freshly cloned
workspace, and each site must treat it as ABSENT, not as a conflict and not as
a repo: validate skips it, the sync planner plans the clone into it, the cache
seed does not use it as a source, and merge verification refuses to build a
DAG from it. The first run of a freshly adopted superproject is the path this must stay green on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gr2.python_cli import gitops, syncops
from gr2.python_cli.app import _merge_verification_targets
from gr2.python_cli.gitops import ensure_repo_cache

from tests.test_materialize_pin import _superproject_with_a_divergent_pair


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)


def _workspace_with_declared_repo(tmp_path: Path, *, member: str) -> tuple[Path, Path]:
    """A workspace that is ITSELF a repository, declaring one repo at ``repos/m``.

    ``member`` is what to leave at the declared path: ``plain`` (a non-empty
    directory), ``placeholder`` (an empty directory), or ``checkout`` (a real
    repository).
    """
    root = tmp_path / "ws"
    _init_repo(root)
    member_path = root / "repos" / "m"
    if member == "placeholder":
        member_path.mkdir(parents=True)
    else:
        member_path.mkdir(parents=True)
        (member_path / "a-file.txt").write_text("not a repository\n")
        if member == "checkout":
            _init_repo(member_path)

    grip = root / ".grip"
    grip.mkdir(parents=True, exist_ok=True)
    (grip / "workspace_spec.toml").write_text(
        'workspace_name = "ws"\n\n'
        '[[repos]]\nname = "m"\npath = "repos/m"\nurl = "https://example.com/m.git"\n'
    )
    return root, member_path


def test_the_helper_answers_three_states(tmp_path: Path) -> None:
    """FIXTURE GUARD for the helper itself. The three answers are the contract
    every site below decides against; if the helper collapses to fewer, the
    sites below read through again. Asserted, not assumed: the enclosing
    repository is the thing the old helper used to answer FOR."""
    root, plain = _workspace_with_declared_repo(tmp_path, member="plain")

    assert gitops.repo_path_state(root) == "repo_root", "the workspace root is a repository"
    assert gitops.repo_path_state(plain) == "neither", (
        "a non-empty plain directory inside a checkout is not a repo and not empty"
    )
    placeholder = tmp_path / "ph"
    placeholder.mkdir()
    assert gitops.repo_path_state(placeholder) == "empty_placeholder", (
        "an empty readable directory is the not-yet-materialized shape"
    )
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / "x").write_text("x")
    os.chmod(unreadable, 0)
    try:
        assert gitops.repo_path_state(unreadable) == "neither", (
            "an unreadable directory is not an empty one; we cannot know"
        )
    finally:
        os.chmod(unreadable, 0o755)
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    assert gitops.repo_path_state(a_file) == "neither", "a file is neither"


def test_a_real_checkout_at_the_declared_path_is_a_repo_root(tmp_path: Path) -> None:
    """THE PAIR. Without this the helper could answer `neither` for real
    repositories and every site would refuse them."""
    _, member = _workspace_with_declared_repo(tmp_path, member="checkout")
    assert gitops.repo_path_state(member) == "repo_root"


# ---------------------------------------------------------------------------
# Site 1: merge verification (app.py:376). The guard's contract is to refuse
# when the DAG is unavailable; the read-through let a plain directory through.
# ---------------------------------------------------------------------------

def test_merge_verification_refuses_a_plain_directory(tmp_path: Path) -> None:
    """THE WITNESS. On dev the guard returned the plain directory as a live
    merge-verification target."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="plain")
    try:
        targets = _merge_verification_targets(root)
        raise AssertionError(f"the guard must refuse a non-repo member path, got {targets}")
    except SystemExit as exc:
        assert "merge-verification DAG is unavailable" in str(exc), str(exc)


def test_merge_verification_refuses_a_placeholder(tmp_path: Path) -> None:
    """THE PLACEHOLDER CASE. An unmaterialized member is not a live DAG target
    either; the guard refuses the same way."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="placeholder")
    try:
        targets = _merge_verification_targets(root)
        raise AssertionError(f"the guard must refuse an unmaterialized member, got {targets}")
    except SystemExit as exc:
        assert "merge-verification DAG is unavailable" in str(exc), str(exc)


def test_merge_verification_accepts_a_materialized_member(tmp_path: Path) -> None:
    """THE PAIR. A real checkout at the declared path is a live target."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="checkout")
    targets = _merge_verification_targets(root)
    assert set(targets) == {"m"}


# ---------------------------------------------------------------------------
# Site 2: the sync planner's shared-repo path (syncops.py:359).
# ---------------------------------------------------------------------------

def _plan_issues_and_ops(workspace_root: Path) -> tuple[list, list]:
    plan = syncops.build_sync_plan(workspace_root, dirty_mode="block", probe_remotes=False)
    issues = [i.code for i in plan.issues if i.scope == "shared_repo"]
    ops = [o.kind for o in plan.operations if o.scope == "shared_repo"]
    return issues, ops


def test_sync_reports_shared_repo_path_conflict_for_a_plain_directory(tmp_path: Path) -> None:
    """THE WITNESS. With the one shared helper, the validator catches the plain
    directory first and the plan carries the conflict as a spec issue; on dev
    the plan scheduled a fetch INTO the plain directory instead."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="plain")
    plan = syncops.build_sync_plan(root, dirty_mode="block", probe_remotes=False)
    codes = [i.code for i in plan.issues]
    ops = [o.kind for o in plan.operations]
    assert "repo_path_conflict" in codes, f"got issues={codes} ops={ops}"
    assert "fetch_shared_repo" not in ops, ops


def test_sync_plans_a_clone_into_a_placeholder(tmp_path: Path) -> None:
    """THE PLACEHOLDER CASE: an empty directory at the declared path is a
    member that is NOT materialized yet, so the planner plans the clone into
    it -- the same answer as a missing path, not a conflict and not a fetch."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="placeholder")
    issues, ops = syncops_plan_scopes(root)
    assert "shared_repo_path_conflict" not in issues, issues
    assert "clone_shared_repo" in ops, ops


def syncops_plan_scopes(workspace_root: Path) -> tuple[list[str], list[str]]:
    plan = syncops.build_sync_plan(workspace_root, dirty_mode="block", probe_remotes=False)
    issues = [i.code for i in plan.issues if i.scope == "shared_repo"]
    ops = [o.kind for o in plan.operations if o.scope == "shared_repo"]
    return issues, ops


def syncops_plan_scopes_alias(workspace_root: Path) -> tuple[list[str], list[str]]:
    return syncops_plan_scopes(workspace_root)


def test_sync_accepts_a_real_checkout_at_the_shared_path(tmp_path: Path) -> None:
    """THE PAIR. A real checkout produces no conflict; the check is noise a
    reader learns to skip otherwise."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="checkout")
    issues, ops = syncops_plan_scopes(root)
    assert "shared_repo_path_conflict" not in issues, issues


# ---------------------------------------------------------------------------
# Site 3: the lane repo path (syncops.py:475). Same shape, lane side.
# ---------------------------------------------------------------------------

def _lane_workspace(tmp_path: Path, *, member: str) -> Path:
    """A workspace whose SPEC path holds a real checkout (so validation passes
    and the planner reaches the lane block), while the LANE repo path holds
    ``member``: lane paths are not spec-declared, so the lane check is the
    live one for them."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="checkout")
    lane_base = root / ".grip" / "state" / "lanes" / "atlas" / "feature"
    (root / ".grip" / "events").mkdir(parents=True, exist_ok=True)
    lane_member = lane_base / "repos" / "m"
    if member == "placeholder":
        lane_member.mkdir(parents=True)
    elif member == "plain":
        lane_member.mkdir(parents=True)
        (lane_member / "a-file.txt").write_text("not a repository\n")
    else:
        _init_repo(lane_member)
    (lane_base / "lane.toml").write_text(
        'lane_name = "feature"\n'
        'owner_unit = "atlas"\n'
        'lane_type = "feature"\n'
        'repos = ["m"]\n'
        'branch_map = {"m" = "feat/x"}\n'
        '[exec_defaults]\nparallelism = "sequential"\nfail_fast = true\ncommands = []\n'
        '[context]\nshared_roots = []\nprivate_roots = []\n'
    )
    return root


def _lane_issues(workspace_root: Path) -> list[str]:
    plan = syncops.build_sync_plan(workspace_root, dirty_mode="block", probe_remotes=False)
    return [i.code for i in plan.issues if i.scope == "lane"]


def _lane_ops(workspace_root: Path) -> list[str]:
    plan = syncops.build_sync_plan(workspace_root, dirty_mode="block", probe_remotes=False)
    return [o.kind for o in plan.operations if o.scope == "lane"]


def test_sync_reports_lane_repo_path_conflict_for_a_plain_directory(tmp_path: Path) -> None:
    """THE WITNESS. On dev the conflict was skipped and the plan answered with
    a FALSE dirty_lane_repo block (repo_dirty answered for the enclosing repo)."""
    root = _lane_workspace(tmp_path, member="plain")
    issues, ops = _lane_issues(root), _lane_ops(root)
    assert "lane_repo_path_conflict" in issues, f"got issues={issues} ops={ops}"
    assert "dirty_lane_repo" not in issues, ops


def test_sync_plans_a_lane_materialize_for_a_placeholder(tmp_path: Path) -> None:
    """THE PLACEHOLDER CASE: an empty lane repo path is a missing checkout."""
    root = _lane_workspace(tmp_path, member="placeholder")
    issues, ops = _lane_issues(root), _lane_ops(root)
    assert "lane_repo_path_conflict" not in issues, issues
    assert "materialize_lane_repo" in ops, ops


def test_sync_accepts_a_real_lane_checkout(tmp_path: Path) -> None:
    """THE PAIR."""
    root = _lane_workspace(tmp_path, member="checkout")
    issues, ops = _lane_issues(root), _lane_ops(root)
    assert "lane_repo_path_conflict" not in issues, issues


# ---------------------------------------------------------------------------
# Site 4: the cache seed's local source (syncops.py:593, gitops.py:310).
# ---------------------------------------------------------------------------

def _seed_source_fixture(tmp_path: Path, *, member: str) -> tuple[Path, Path, Path, str]:
    """A workspace whose repo cache seeds from a REAL local file:// remote, so
    the seed can succeed from the URL once a non-repo is no longer trusted as
    a source."""
    root, member_path = _workspace_with_declared_repo(tmp_path, member=member)
    src = tmp_path / "src.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(src)], check=True)
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "one.txt").write_text("one\n")
    _run("add", "-A", cwd=seed)
    _run("commit", "-qm", "one", cwd=seed)
    _run("push", "-q", str(src), "main", cwd=seed)
    return root, member_path, src, f"file://{src}"


def test_cache_seed_ignores_a_plain_directory_as_a_local_source(
    tmp_path: Path, monkeypatch
) -> None:
    """THE WITNESS. On dev the plain directory was passed as ``local_source``
    and the seed failed with git's 'repository does not exist'."""
    root, _, src, url = _seed_source_fixture(tmp_path, member="plain")
    cache = root / ".grip" / "cache" / "repos" / "m.git"
    created = ensure_repo_cache(url, cache, local_source=root / "repos" / "m")
    assert created is True, f"the seed must succeed from the URL, cache={cache.exists()}"
    heads = _run("rev-parse", "HEAD", cwd=cache).stdout.strip()
    assert heads, "the seeded cache has a head"


def test_cache_seed_ignores_a_placeholder_as_a_local_source(tmp_path: Path) -> None:
    """THE PLACEHOLDER CASE: an empty directory is not a usable source."""
    root, _, src, url = _seed_source_fixture(tmp_path, member="placeholder")
    cache = root / ".grip" / "cache" / "repos" / "m.git"
    created = ensure_repo_cache(url, cache, local_source=root / "repos" / "m")
    assert created is True, f"the seed must succeed from the URL, cache={cache.exists()}"


def test_cache_seed_uses_a_real_repo_root_as_a_local_source(tmp_path: Path) -> None:
    """THE PAIR: a real checkout at the declared path IS a usable local source,
    or the seam defense threw away a feature."""
    root, _, src, url = _seed_source_fixture(tmp_path, member="checkout")
    cache = root / ".grip" / "cache" / "repos" / "m.git"
    created = ensure_repo_cache(url, cache, local_source=root / "repos" / "m")
    assert created is True


# ---------------------------------------------------------------------------
# Site 5: the lane create source (app.py:181). On an adopted superproject the
# declared path is the EMPTY placeholder; asking the root path for its origin
# read through to the WORKSPACE's origin and the provenance check refused.
# ---------------------------------------------------------------------------

def _adopted_superproject(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A plain clone of a superproject, adopted, with members materialized into
    the unit -- the adopted workspace's first run -- plus the root's pins."""
    root, pins = _superproject_with_a_divergent_pair(tmp_path)
    plain = tmp_path / "plain"
    subprocess.run(["git", "clone", "-q", str(root), str(plain)], check=True)
    for member in ("diverging", "converging"):
        placeholder = plain / member
        assert placeholder.is_dir() and not any(placeholder.iterdir()), (
            f"precondition: {member} is an EMPTY placeholder after a plain clone"
        )
    return plain, pins


def _lane_cli(*args: str) -> tuple[int, str]:
    from tests.conftest import make_cli_runner
    from gr2.python_cli.app import app
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def test_lane_create_sources_a_placeholder_member_from_the_unit_copy(tmp_path: Path) -> None:
    """THE WITNESS, the whole first run. On the unfixed code lane create failed
    rc 1 with the provenance refusal: the lane source was the EMPTY placeholder
    at the root, whose origin read through to the workspace root's own remote."""
    plain, pins = _adopted_superproject(tmp_path)
    rc, out = _lane_cli("workspace", "init", str(plain), "--from-superproject")
    assert rc == 0, out
    rc, out = _lane_cli("spec", "validate", str(plain))
    assert rc == 0, out
    rc, out = _lane_cli("workspace", "materialize", str(plain), "--yes")
    assert rc == 0, out
    unit = plain / "agents" / "default" / "home"
    for member, want in pins.items():
        got = _run("rev-parse", "HEAD", cwd=unit / member).stdout.strip()
        assert got == want, f"{member} landed on {got[:12]}, the root pins {want[:12]}"

    rc, out = _lane_cli(
        "lane", "create", str(plain), "default", "feat-x",
        "--repos", "diverging,converging", "--branch", "feat/x",
    )
    assert rc == 0, f"lane create must succeed with the unit's materialized source: {out}"
    for member in pins:
        lane_repo = plain / ".grip" / "state" / "lanes" / "default" / "feat-x" / "repos" / member
        head = _run("rev-parse", "HEAD", cwd=lane_repo)
        assert head.returncode == 0, f"lane checkout exists for {member}: {out}"
        origin = _run("config", "remote.origin.url", cwd=lane_repo).stdout.strip()
        unit_origin = _run("config", "remote.origin.url", cwd=unit / member).stdout.strip()
        assert origin == unit_origin and origin, (
            f"{member}'s lane origin {origin!r} must be the member's URL, not the workspace root's"
        )


def test_lane_create_refuses_when_nothing_is_materialized(tmp_path: Path) -> None:
    """THE PLACEHOLDER'S SIBLING: a declared path that is neither a repo root
    nor a placeholder (a non-empty plain directory) cannot be a lane source;
    the refusal tells the reader what to run."""
    root, _ = _workspace_with_declared_repo(tmp_path, member="plain")
    # the lane flow resolves the unit before materializing sources; seed one
    # so the refusal the witness checks is the source refusal, not the unit's
    spec_path = root / ".grip" / "workspace_spec.toml"
    spec_path.write_text(spec_path.read_text() + '[[units]]\nname = "default"\npath = "agents/default/home"\nrepos = ["m"]\n')
    rc, out = _lane_cli("lane", "create", str(root), "default", "feat-x", "--repos", "m", "--branch", "feat/x")
    assert rc != 0, out
    assert "materialize" in out, out
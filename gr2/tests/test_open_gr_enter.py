"""Project-tier open-gr --enter (R2 Exact Work Stream 2 step 4): one review-kind
gr commit opens one exact multi-repo review; exit restores the prior lane + cwd."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

import typer

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip, open_gr_review, project_review


@pytest.fixture(autouse=True)
def _isolated_review_cache(tmp_path, monkeypatch):
    """Each test gets its OWN persistent review-mirror cache. The cache is keyed by
    repo NAME (globally unique in production: recall/grip/config), but fixture repos
    reuse alpha/beta/gamma across tests, so without isolation a stale mirror from an
    earlier test would answer for a later one. Points SYNAPT_REVIEW_CACHE_ROOT (the
    same override review-clone.sh honors) at a per-test dir; unsets the profile dir
    so fixtures have no gripspace sparse fallback (whole tree, blobless)."""
    monkeypatch.setenv("SYNAPT_REVIEW_CACHE_ROOT", str(tmp_path / "review-cache"))
    monkeypatch.delenv("SYNAPT_REVIEW_PROFILE_DIR", raising=False)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _source(root: Path, name: str) -> tuple[Path, str, str]:
    """A source repo pushed to a bare origin: returns (source, base_sha, head_sha)."""
    origin = root / f"{name}.git"
    _git(root, "init", "--bare", "-b", "main", str(origin))
    source = root / name
    _git(root, "clone", "-q", str(origin), str(source))
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "config", "user.name", "t")
    (source / "README.md").write_text(f"{name} base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "push", "-q", "origin", "main")
    base = _git(source, "rev-parse", "main")
    _git(source, "checkout", "-q", "-b", f"review/{name}")
    (source / "review.txt").write_text(f"{name} review\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "review")
    _git(source, "push", "-q", "origin", f"review/{name}")
    head = _git(source, "rev-parse", f"review/{name}")
    _git(source, "checkout", "-q", "main")
    return source, base, head


def _world(tmp_path: Path):
    """workspace (grip-inited) + 3 sources + a HOME lane entered + a prior cwd."""
    workspace = tmp_path / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    sources = {name: _source(tmp_path, name) for name in ("alpha", "beta", "gamma")}
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m1"\n\n'
        + "\n".join(
            f'[[repos]]\nname = "{name}"\npath = "sources/{name}"\n'
            f'url = "{_git(src[0], "remote", "get-url", "origin")}"\n'
            for name, src in sources.items()
        )
        + '\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["alpha", "beta", "gamma"]\n'
    )
    grip.grip_init(workspace)
    prior_cwd = tmp_path / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos="alpha", branch="main", source="test", default_commands=[],
    ))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False,
    ))
    return workspace, sources, prior_cwd


def _review_kind_commit(workspace: Path, sources: dict) -> tuple[str, dict]:
    pins = [
        project_review.ProjectReviewPin(
            key=name, repo=f"local:{src[0]}", path=f"repos/{name}", base=src[1], head=src[2]
        )
        for name, src in sources.items()
    ]
    spec = project_review.make_spec(workspace, pins)
    return spec.grip_commit, {name: (src[0], f"review/{name}") for name, src in sources.items()}


def test_open_gr_enter_materializes_pins_enters_and_writes_a_receipt(tmp_path: Path) -> None:
    workspace, sources, prior_cwd = _world(tmp_path)
    gr_commit, srcmap = _review_kind_commit(workspace, sources)
    before = lanes.current_lane_file(workspace, "atlas").read_bytes()

    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, srcmap,
        prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "opened"
    # the three pinned heads are materialized
    for name in sources:
        assert (outcome.review_root / "repos" / name / ".git").is_dir()
    # the agent is dropped into the review lane (current lane changed)
    assert lanes.current_lane_file(workspace, "atlas").read_bytes() != before
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "review-m1"
    # the receipt names the gr commit and the per-repo base/head
    import json
    receipt = json.loads(open_gr_review.open_gr_receipt_path(outcome.review_root).read_text())
    assert receipt["gr_commit"] == gr_commit
    assert receipt["prior_lane"] == "home"
    assert receipt["prior_cwd"] == str(prior_cwd)
    by_key = {r["key"]: r for r in receipt["repos"]}
    for name, src in sources.items():
        assert by_key[name]["base"] == src[1]
        assert by_key[name]["head"] == src[2]


def test_exit_restores_the_prior_lane_and_cwd(tmp_path: Path) -> None:
    workspace, sources, prior_cwd = _world(tmp_path)
    gr_commit, srcmap = _review_kind_commit(workspace, sources)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, srcmap, prior_cwd=prior_cwd, allow_local=True,
    )
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "review-m1"

    result = open_gr_review.exit_gr_review(workspace, "atlas", outcome.review_root, actor="agent:atlas")
    # the prior lane is restored and the prior cwd is returned
    assert result.restored_lane == "home"
    assert result.restored_cwd == str(prior_cwd)
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_open_gr_enter_refuses_a_non_review_kind_commit(tmp_path: Path) -> None:
    # Control 1: a workspace-kind commit is refused before any materialization.
    workspace, sources, prior_cwd = _world(tmp_path)
    wrong = grip.create_workspace_commit(workspace, [
        {"key": name, "remote": f"https://example.invalid/{name}.git", "path": f"repos/{name}",
         "commit": src[2], "base": src[1]}
        for name, src in sources.items()
    ])
    _, srcmap = _review_kind_commit(workspace, sources)
    with pytest.raises(grip.GripCorruptError):
        open_gr_review.open_gr_enter(
            workspace, "atlas", "review-m1", wrong, srcmap, prior_cwd=prior_cwd, allow_local=True,
        )
    # nothing was materialized and the agent stayed in home
    assert not (workspace / "reviews" / "atlas" / "review-m1").exists()
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_open_gr_enter_resolves_sources_from_the_recorded_remote(tmp_path: Path) -> None:
    # Step 6: no hand-passed transport map. open-gr resolves each source from the
    # pin's RECORDED REMOTE (pin.repo), clones the pinned head, and opens the same
    # exact review. This is the production shape -- the review-kind commit is the
    # only input.
    workspace, sources, prior_cwd = _world(tmp_path)
    gr_commit, _unused_srcmap = _review_kind_commit(workspace, sources)

    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, None,  # sources resolved from pins
        prior_cwd=prior_cwd, allow_local=True, staging_dir=tmp_path / "staging",
    )
    assert outcome.status == "opened"
    for name in sources:
        assert (outcome.review_root / "repos" / name / ".git").is_dir()
        # each materialized head equals the pinned head, reached via the recorded remote
        assert _git(outcome.review_root / "repos" / name, "rev-parse", "HEAD") == sources[name][2]
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "review-m1"


def test_resolve_sources_from_pins_refuses_a_remote_missing_the_head(tmp_path: Path) -> None:
    # Control: a recorded remote that does not carry the pinned head is refused at
    # resolve time, BEFORE any review lane opens; the agent stays in home.
    workspace, sources, prior_cwd = _world(tmp_path)
    pins = []
    for name, src in sources.items():
        head = src[2] if name != "beta" else "b" * 40  # beta's head is not on its remote
        pins.append(project_review.ProjectReviewPin(
            key=name, repo=f"local:{src[0]}", path=f"repos/{name}", base=src[1], head=head))
    gr_commit = project_review.make_spec(workspace, pins).grip_commit

    with pytest.raises(open_gr_review.OpenGrReviewError, match="does not carry head"):
        open_gr_review.open_gr_enter(
            workspace, "atlas", "review-m1", gr_commit, None,
            prior_cwd=prior_cwd, allow_local=True, staging_dir=tmp_path / "staging",
        )
    assert not (workspace / "reviews" / "atlas" / "review-m1").exists()
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_open_gr_enter_refuses_a_pin_head_missing_from_its_repo(tmp_path: Path) -> None:
    # Control 2: a pin whose head is not a commit in its source is refused BEFORE
    # any materialization (open_project_review preflights every pin).
    workspace, sources, prior_cwd = _world(tmp_path)
    pins = []
    for name, src in sources.items():
        head = src[2] if name != "beta" else "b" * 40  # beta's head does not exist
        pins.append(project_review.ProjectReviewPin(
            key=name, repo=f"local:{src[0]}", path=f"repos/{name}", base=src[1], head=head))
    gr_commit = project_review.make_spec(workspace, pins).grip_commit
    srcmap = {name: (src[0], f"review/{name}") for name, src in sources.items()}

    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, srcmap, prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "refused"
    assert not (workspace / "reviews" / "atlas" / "review-m1" / "repos").exists()
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def _prepush_world(tmp_path: Path):
    """A one-repo world whose review head is committed LOCALLY but NOT pushed to the
    repo's origin (a pre-push / gated head). Returns (workspace, source, base, head,
    prior_cwd). The origin (recorded remote) carries base but not head, exactly like a
    gated review branch that the freeze asserts is ABSENT on the remote."""
    root = tmp_path
    origin = root / "alpha.git"
    _git(root, "init", "--bare", "-b", "main", str(origin))
    source = root / "alpha"
    _git(root, "clone", "-q", str(origin), str(source))
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "config", "user.name", "t")
    (source / "README.md").write_text("alpha base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "push", "-q", "origin", "main")
    base = _git(source, "rev-parse", "main")
    # the review head: committed locally, NEVER pushed -> origin does not carry it
    (source / "review.txt").write_text("alpha review\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "review")
    head = _git(source, "rev-parse", "HEAD")

    workspace = root / "workspace"
    (workspace / ".grip").mkdir(parents=True)
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m1"\n\n'
        f'[[repos]]\nname = "alpha"\npath = "sources/alpha"\nurl = "{str(origin)}"\n'
        '\n[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["alpha"]\n'
    )
    grip.grip_init(workspace)
    prior_cwd = root / "home"
    prior_cwd.mkdir()
    lanes.create_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home", type="feature",
        repos="alpha", branch="main", source="test", default_commands=[],
    ))
    lanes.enter_lane(argparse.Namespace(
        workspace_root=workspace, owner_unit="atlas", lane_name="home",
        actor="agent:atlas", notify_channel=False, recall=False,
    ))
    return workspace, source, base, head, prior_cwd


def _prepush_gr_commit(workspace: Path, source: Path, base: str, head: str) -> str:
    pin = project_review.ProjectReviewPin(
        key="alpha", repo=f"local:{source}", path="repos/alpha", base=base, head=head)
    return project_review.make_spec(workspace, [pin]).grip_commit


def test_local_source_materializes_a_pre_push_head_via_the_sparse_path(tmp_path: Path) -> None:
    # A pre-push head is absent on the recorded remote (a gated review head is never on
    # the remote), so the mirror
    # seeded from the remote lacks it. Naming a LOCAL clone that holds it tops the
    # mirror up and the SAME blobless ephemeral path runs -- NOT a full --sources-json
    # clone. Measured on recall: 595M full -> 32M blobless+sparse.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)

    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, None,  # sources None -> ephemeral
        prior_cwd=prior_cwd, allow_local=True, local_sources={"alpha": source},
    )
    assert outcome.status == "opened", outcome
    lane_repo = outcome.review_root / "repos" / "alpha"
    assert _git(lane_repo, "rev-parse", "HEAD") == head  # the pre-push head materialized
    # the ephemeral path ran: a blobless partial clone, not a full copy.
    assert _git(lane_repo, "config", "remote.origin.partialclonefilter") == "blob:none"
    import json
    receipt = json.loads(open_gr_review.open_gr_receipt_path(outcome.review_root).read_text())
    assert receipt["lane_kind"] == "review-ephemeral"


def test_local_source_ephemeral_review_tree_is_removed_on_exit(tmp_path: Path) -> None:
    # The pre-push fix makes the review ephemeral, and exit-gr removes an ephemeral
    # tree (rm -rf) -- so the fix also retires the "exit-gr leaves the full tree on
    # disk" finding, which was a symptom of the NON-ephemeral --sources-json fallback.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, None,
        prior_cwd=prior_cwd, allow_local=True, local_sources={"alpha": source},
    )
    assert outcome.status == "opened"
    review_root = outcome.review_root
    assert review_root.exists()
    open_gr_review.exit_gr_review(workspace, "atlas", review_root, actor="agent:atlas")
    assert not review_root.exists()  # disposable ephemeral tree cleaned up
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_pre_push_head_without_a_local_source_still_refuses(tmp_path: Path) -> None:
    # Control: without --local-source, a pre-push head is still refused at resolve
    # time (the remote does not carry it), and the agent stays in home. The fix adds
    # a path; it does not weaken the refusal when no local source is named.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)
    with pytest.raises(open_gr_review.OpenGrReviewError, match="does not carry head"):
        open_gr_review.open_gr_enter(
            workspace, "atlas", "review-m1", gr_commit, None,
            prior_cwd=prior_cwd, allow_local=True,  # no local_sources
        )
    assert not (workspace / "reviews" / "atlas" / "review-m1").exists()
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_local_source_topup_does_not_clobber_mirror_refs_heads(tmp_path: Path) -> None:
    # The top-up writes the pinned head ONLY into refs/localsrc/<key>/, NEVER into the
    # mirror's own refs/heads/*: a desk clone's stale branch tips must not become the
    # shared mirror's branches. This defends the namespacing (a refspec that wrote into
    # refs/heads/* would pass the other tests but corrupt the shared cache).
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    assert base != head  # fixture sanity: the local branch tip differs from the remote
    gr_commit = _prepush_gr_commit(workspace, source, base, head)
    open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, None,
        prior_cwd=prior_cwd, allow_local=True, local_sources={"alpha": source},
    )
    mirror = open_gr_review.review_cache_root() / "alpha.git"
    # the mirror's own main is UNTOUCHED -- still the remote's sha (base), not the
    # local desk clone's tip (head).
    assert _git(mirror, "rev-parse", "refs/heads/main") == base
    # the head is present, anchored under the namespaced ref (scoped to the head sha).
    assert _git(mirror, "rev-parse", "refs/localsrc/alpha/head") == head


def test_local_source_nonexistent_path_raises_naming_the_path(tmp_path: Path) -> None:
    # A bad --local-source path must FAIL naming the path, not silently swallow the
    # fetch error and fall through to the generic "publish the head" refusal (which
    # would wrongly blame the remote for a local-source typo).
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)
    bad = tmp_path / "no-such-clone-here"
    with pytest.raises(open_gr_review.OpenGrReviewError, match="no-such-clone-here"):
        open_gr_review.open_gr_enter(
            workspace, "atlas", "review-m1", gr_commit, None,
            prior_cwd=prior_cwd, allow_local=True, local_sources={"alpha": bad},
        )
    assert not (workspace / "reviews" / "atlas" / "review-m1").exists()
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_carried_range_open_reconstructs_sparse_without_a_local_source(tmp_path: Path) -> None:
    # Row 1 end to end: a project-review commit that CARRIES the range reconstructs
    # the pre-push head from the commit alone -- no --local-source, no --sources-json,
    # no head on any remote. The review lane is still blobless+sparse (materialized at
    # the reconstructed head, tree-equal to the pinned head); the receipt records both.
    import json
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    remote = _git(source, "remote", "get-url", "origin")
    range_patch = subprocess.run(
        ["git", "format-patch", f"{base}..{head}", "--stdout"],
        cwd=source, text=True, capture_output=True, check=True).stdout
    gr_commit = grip.create_project_review_commit(
        workspace,
        [{"key": "alpha", "repo": remote, "path": "repos/alpha", "base": base, "head": head}],
        ranges={"alpha": range_patch},
    )

    # open with NEITHER sources nor local_sources: the carried range is the only input.
    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, None,
        prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "opened", outcome
    lane_repo = outcome.review_root / "repos" / "alpha"
    # blobless+sparse ephemeral (not a full clone): the sparse review path was taken,
    assert _git(lane_repo, "config", "remote.origin.partialclonefilter") == "blob:none"
    # the reconstructed tree is the range's tree (the review's added file is present).
    assert (lane_repo / "review.txt").read_text() == "alpha review\n"
    # receipt records the pinned head AND the reconstructed head (row 2's before/after).
    receipt = json.loads(open_gr_review.open_gr_receipt_path(outcome.review_root).read_text())
    row = {r["key"]: r for r in receipt["repos"]}["alpha"]
    assert row["head"] == head                       # pinned (pre-push) head, unchanged
    assert row["reconstructed_head"] != head          # git am re-stamps -> different sha
    # the lane actually checks out the reconstructed head (tree-equal to the pinned one).
    assert _git(lane_repo, "rev-parse", "HEAD") == row["reconstructed_head"]


def _carried_range_gr_commit(workspace: Path, source: Path, base: str, head: str) -> str:
    remote = _git(source, "remote", "get-url", "origin")
    range_patch = subprocess.run(
        ["git", "format-patch", f"{base}..{head}", "--stdout"],
        cwd=source, text=True, capture_output=True, check=True).stdout
    return grip.create_project_review_commit(
        workspace,
        [{"key": "alpha", "repo": remote, "path": "repos/alpha", "base": base, "head": head}],
        ranges={"alpha": range_patch},
    )


def _redirect_mkdtemp(monkeypatch, parent: Path) -> list:
    """Force open_gr_review's scratch reconstruct clone under a test-owned dir and
    record every path it creates, so a test can assert the scratch was removed. Returns
    the list the wrapper appends created paths to."""
    import tempfile
    parent.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    real = tempfile.mkdtemp

    def rec(*args, **kwargs):
        # Only redirect open_gr_review's reconstruct scratch (prefix "gr2-reconstruct-");
        # pass every other mkdtemp through untouched, including the positional
        # mkdtemp(None, None, None) that tempfile.TemporaryDirectory issues inside the
        # reconstruction itself -- forcing dir on that call collides with its positional dir.
        prefix = kwargs.get("prefix") or (args[1] if len(args) > 1 else None)
        if prefix != "gr2-reconstruct-":
            return real(*args, **kwargs)
        p = real(prefix="gr2-reconstruct-", dir=str(parent))
        created.append(Path(p))
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", rec)
    return created


def test_carried_range_scratch_clone_is_removed_on_the_success_path(tmp_path: Path, monkeypatch) -> None:
    # The scratch is a FULL clone (per pre-push open, on a host that can be near-full),
    # so it must not survive the call. Redirect mkdtemp under a dir the test owns, open a
    # carried-range review, and assert the scratch is gone -- the guard that a future
    # emptying of the cleanup would break (that mutant passed every OTHER test).
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _carried_range_gr_commit(workspace, source, base, head)
    scr = tmp_path / "scratch-parent"
    created = _redirect_mkdtemp(monkeypatch, scr)

    outcome = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-m1", gr_commit, None,
        prior_cwd=prior_cwd, allow_local=True,
    )
    assert outcome.status == "opened", outcome
    assert created, "the carried-range open never created a reconstruct scratch"
    assert not created[0].exists(), "scratch reconstruct clone was left on disk after open"
    assert list(scr.glob("gr2-reconstruct-*")) == []  # nothing of ours remains under the parent


def test_carried_range_scratch_clone_is_removed_when_reconstruction_fails(tmp_path: Path, monkeypatch) -> None:
    # The scratch is created (mkdtemp) BEFORE reconstruction runs, and reconstruction can
    # raise (a range that does not apply -> GripReviewRefused, a base unreachable on the
    # remote, a tree mismatch). The cleanup must cover THAT exit path too, not only a
    # clean open -- otherwise a failed reconstruct leaks the full clone. Make reconstruct
    # raise and assert the scratch is still removed and the error propagates.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _carried_range_gr_commit(workspace, source, base, head)
    scr = tmp_path / "scratch-parent"
    created = _redirect_mkdtemp(monkeypatch, scr)

    def boom(*args, **kwargs):
        raise grip.GripReviewRefused("reconstruct_failed", "alpha", "range does not apply")

    monkeypatch.setattr(open_gr_review.grip, "reconstruct_project_review_lane", boom)

    with pytest.raises(grip.GripReviewRefused):
        open_gr_review.open_gr_enter(
            workspace, "atlas", "review-m1", gr_commit, None,
            prior_cwd=prior_cwd, allow_local=True,
        )
    assert created, "mkdtemp was not called before the reconstruction failure"
    assert not created[0].exists(), "scratch clone leaked when reconstruction raised"
    assert list(scr.glob("gr2-reconstruct-*")) == []


# ---- Part 2: the CLI materialize verb (review open-project) routes through open_gr_enter ----

def _review_help(command_name: str) -> str:
    cmd = next(c for c in gr2_app.review_app.registered_commands if c.name == command_name)
    return (cmd.help or (cmd.callback.__doc__ or "")).strip()


def test_review_open_project_cli_materializes_from_recorded_remote_then_exits(tmp_path: Path) -> None:
    # One CLI invocation materializes the project-review-KIND commit's pinned heads
    # from their recorded remotes and enters the review lane; exit-gr restores.
    workspace, sources, prior_cwd = _world(tmp_path)
    gr_commit, _unused = _review_kind_commit(workspace, sources)

    gr2_app.review_open_project(
        workspace, gr_commit, "atlas", "review-m1",
        enter=True, sources_json=None, local_source=None, prior_cwd=prior_cwd, allow_local=True, json_output=False,
    )
    review_root = workspace / "reviews" / "atlas" / "review-m1"
    for name in sources:
        assert (review_root / "repos" / name / ".git").is_dir()
        assert _git(review_root / "repos" / name, "rev-parse", "HEAD") == sources[name][2]
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "review-m1"

    gr2_app.review_exit_gr(workspace, "atlas", review_root, actor="agent:atlas", json_output=False)
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_review_open_project_cli_local_source_materializes_a_pre_push_head(tmp_path: Path) -> None:
    # `open-project --local-source alpha=<path>` materializes a pre-push
    # head via the blobless ephemeral path (no hand-authored --sources-json, no full
    # clone). This retires the hand-authored-sources.json CLI exit point.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)

    gr2_app.review_open_project(
        workspace, gr_commit, "atlas", "review-m1",
        enter=True, sources_json=None, local_source=[f"alpha={source}"],
        prior_cwd=prior_cwd, allow_local=True, json_output=False,
    )
    review_root = workspace / "reviews" / "atlas" / "review-m1"
    lane_repo = review_root / "repos" / "alpha"
    assert _git(lane_repo, "rev-parse", "HEAD") == head
    assert _git(lane_repo, "config", "remote.origin.partialclonefilter") == "blob:none"
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "review-m1"


def test_review_open_project_cli_rejects_local_source_without_equals(tmp_path: Path) -> None:
    # Atlas's R1 non-blocking find (folded here): --local-source must be key=PATH; a
    # value with no "=" is a usage error, not a silent skip or a crash.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)
    with pytest.raises(typer.BadParameter):
        gr2_app.review_open_project(
            workspace, gr_commit, "atlas", "review-m1",
            enter=True, sources_json=None, local_source=["alpha-no-equals-sign"],
            prior_cwd=prior_cwd, allow_local=True, json_output=False,
        )


def test_review_open_project_cli_rejects_sources_json_with_local_source(tmp_path: Path) -> None:
    # The two source modes are mutually exclusive (full vs sparse); naming both is a
    # usage error, not a silent precedence.
    workspace, source, base, head, prior_cwd = _prepush_world(tmp_path)
    gr_commit = _prepush_gr_commit(workspace, source, base, head)
    with pytest.raises(typer.BadParameter):
        gr2_app.review_open_project(
            workspace, gr_commit, "atlas", "review-m1",
            enter=True, sources_json=Path("x.json"), local_source=[f"alpha={source}"],
            prior_cwd=prior_cwd, allow_local=True, json_output=False,
        )


def test_review_open_project_cli_refuses_non_review_kind_naming_the_kind(tmp_path: Path, capsys) -> None:
    # Control: a workspace-KIND commit is refused, and the refusal names the kind found.
    workspace, sources, prior_cwd = _world(tmp_path)
    wrong = grip.create_workspace_commit(workspace, [
        {"key": name, "remote": f"https://example.invalid/{name}.git", "path": f"repos/{name}",
         "commit": src[2], "base": src[1]}
        for name, src in sources.items()
    ])
    with pytest.raises(typer.Exit) as exc:
        gr2_app.review_open_project(
            workspace, wrong, "atlas", "review-m1",
            enter=True, sources_json=None, local_source=None, prior_cwd=prior_cwd, allow_local=True, json_output=False,
        )
    assert exc.value.exit_code == 2
    err = capsys.readouterr().err
    assert "gr2-workspace/v1" in err and "project review" in err
    assert not (workspace / "reviews" / "atlas" / "review-m1").exists()
    assert lanes.require_current_lane(workspace, "atlas")["lane_name"] == "home"


def test_review_open_project_requires_enter(tmp_path: Path) -> None:
    workspace, sources, prior_cwd = _world(tmp_path)
    gr_commit, _unused = _review_kind_commit(workspace, sources)
    with pytest.raises(typer.BadParameter):
        gr2_app.review_open_project(
            workspace, gr_commit, "atlas", "review-m1",
            enter=False, sources_json=None, local_source=None, prior_cwd=prior_cwd, allow_local=True, json_output=False,
        )


def test_derived_and_hand_authored_transports_yield_the_same_review_identity(tmp_path: Path) -> None:
    # Step 6 derivation vs the older hand-authored --sources-json escape hatch: the
    # transport must not change WHICH heads get reviewed. Opening the SAME review-kind
    # gr commit both ways -- once with a hand-passed source map (clones normally/full),
    # once with sources=None (resolved from the pins through the shared mirror,
    # blobless+sparse) -- produces receipts whose IDENTITY is byte-for-byte equal.
    # The transport-DESCRIPTIVE fields differ BY DESIGN and are asserted to differ so a
    # later change cannot quietly "fix" them equal: lane_kind (materialized vs
    # review-ephemeral) and the per-repo {mirror, fetched_tip} a reviewer uses to tell a
    # mirror-backed ephemeral open from a full clone. The receipt carries NO timestamp,
    # so identity equality is exact, not timestamp-tolerant. The refusal mutant -- a pin
    # whose head the mirror lacks refuses BEFORE any lane opens, never falling back to
    # the desk or a full clone -- is test_resolve_sources_from_pins_refuses_a_remote_
    # missing_the_head above (:178).
    import json

    workspace, sources, prior_cwd = _world(tmp_path)
    gr_commit, srcmap = _review_kind_commit(workspace, sources)

    hand = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-hand", gr_commit, srcmap, prior_cwd=prior_cwd, allow_local=True,
    )
    assert hand.status == "opened"
    r_hand = json.loads(open_gr_review.open_gr_receipt_path(hand.review_root).read_text())
    open_gr_review.exit_gr_review(workspace, "atlas", hand.review_root, actor="agent:atlas")

    derived = open_gr_review.open_gr_enter(
        workspace, "atlas", "review-derived", gr_commit, None,  # resolved from the pins
        prior_cwd=prior_cwd, allow_local=True, staging_dir=tmp_path / "staging",
    )
    assert derived.status == "opened"
    r_der = json.loads(open_gr_review.open_gr_receipt_path(derived.review_root).read_text())

    # identity: the SAME review, regardless of transport
    def identity(r: dict) -> dict:
        return {
            "gr_commit": r["gr_commit"],
            "owner_unit": r["owner_unit"],
            "prior_lane": r["prior_lane"],
            "prior_cwd": r["prior_cwd"],
            "repos": sorted((x["key"], x["base"], x["head"]) for x in r["repos"]),
        }

    assert identity(r_hand) == identity(r_der)
    # no timestamp field anywhere -> identity equality is exact, not tolerance-based
    assert not any(k for k in r_hand if "time" in k or "stamp" in k or "date" in k)
    assert not any(k for k in r_der if "time" in k or "stamp" in k or "date" in k)

    # transport-descriptive fields differ BY DESIGN -- pin the difference so nobody
    # later collapses them equal
    assert r_hand["lane_kind"] == "materialized"
    assert r_der["lane_kind"] == "review-ephemeral"
    hand_by_key = {x["key"]: x for x in r_hand["repos"]}
    der_by_key = {x["key"]: x for x in r_der["repos"]}
    for name in sources:
        # hand-authored full clone records no mirror provenance
        assert "mirror" not in hand_by_key[name] and "fetched_tip" not in hand_by_key[name]
        # derived-from-mirror records both, so a reviewer can spot a stale mirror
        assert "mirror" in der_by_key[name] and "fetched_tip" in der_by_key[name]


def test_both_review_verbs_help_names_commit_kind_and_path() -> None:
    op = _review_help("open-project")
    og = _review_help("open-gr")
    assert "MATERIALIZE" in op and "project-review-KIND" in op
    assert "RECONSTRUCT" in og and "review-BIND" in og
    # each verb points at the other so the fork is legible from --help alone
    assert "open-gr" in op and "open-project" in og
    assert "exit-gr" in _review_help("exit-gr").lower() or "exit" in _review_help("exit-gr").lower()


def test_bind_help_names_accepted_remote_path_forms() -> None:
    # bind reads --remote with git inside the workspace's .grip store, and the
    # reconstruction clone cannot resolve a user-repo remote name either, so
    # only a URL or an absolute path works end to end; the help must say that,
    # and the earlier help text's false claims must stay gone
    cmd = next(c for c in gr2_app.review_app.registered_commands if c.name == "bind")
    remote = next(d for d in cmd.callback.__defaults__ if (d.help or "").startswith("Remote URL"))
    text = remote.help or ""
    assert "URL" in text
    assert "absolute" in text
    # the earlier help text's false claims, asserted absent (not the words the
    # true text uses)
    assert "cwd-relative" not in text
    assert "configured remote name" not in text

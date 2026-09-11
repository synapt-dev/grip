"""Repo-tier review primitive: `gr review open`/`close`.

A THIN verb over the grip#807 reference-clone seam
(``ensure_lane_checkout`` → ``materialize_lane_clone``), with the review
behavior in the orchestration:

* a wrong head REFUSES (verdict binding), never warns;
* import resolution is PRINTED at open (the stale-editable-install / PYTHONPATH
  trap — the reviewer must see which tree their run imports);
* cwd-containment is asserted BEFORE any dispatched execution (a child-routing
  defect: a dispatched child must not run against the base tree);
* the review record is EXACTLY the (repo, base pin, review head) triple — a
  one-entry pin-delta, nothing less — so the project tier can adopt it without a
  second pin spelling.

The witnesses travel the real functions and read the on-disk lane, not a mock.
Local bare repos stand in for origin so the URL-derivation path is exercised;
the PR head is a branch seeded only in the source checkout.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from gr2.python_cli import clone_exec
from gr2.python_cli.gitops import remote_origin_url
from gr2.python_cli.review import (
    ReviewError,
    ReviewRecord,
    assert_cwd_contained,
    canonical_source_identity,
    close_review_lane,
    host_pr_head_oid,
    open_review_lane,
    review_record_path,
    run_in_review_lane,
)

_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")


def _run(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_source(root: Path, name: str) -> Path:
    """A bare 'origin' plus a source checkout with one pushed commit on main."""
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
    return source


def _seed_pr_head(source: Path, review_branch: str, text: str) -> str:
    """Create the review branch at a fresh commit, only in the source checkout.
    Returns the head sha. Leaves the source back on main."""
    _run(source, "checkout", "-q", "-b", review_branch)
    (source / f"{review_branch.replace('/', '_')}.txt").write_text(text)
    _run(source, "add", ".")
    _run(source, "commit", "-q", "-m", f"work on {review_branch}")
    sha = _run(source, "rev-parse", "HEAD")
    _run(source, "checkout", "-q", "main")
    return sha


@pytest.fixture
def review_world(tmp_path: Path):
    """A source repo with a seeded PR head, plus resolved base/head pins and the
    lane destination the review would materialize into."""
    source = _init_source(tmp_path, "grip")
    review_branch = "pr/7"
    head_sha = _seed_pr_head(source, review_branch, "pr work\n")
    base_sha = _run(source, "rev-parse", "main")
    lane_root = tmp_path / "lane"
    lane = lane_root / "repos" / "grip"
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    return {
        "source": source,
        "review_branch": review_branch,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "lane": lane,
        "lane_root": lane_root,
        "workspace_root": workspace_root,
    }


def _open(world, **overrides):
    kwargs = dict(
        source_repo_root=world["source"],
        review_branch=world["review_branch"],
        expected_head_sha=world["head_sha"],
        base_sha=world["base_sha"],
        lane_repo_root=world["lane"],
        workspace_root=world["workspace_root"],
        allow_local=True,  # the test origin is a local bare repo, not a GitHub URL
    )
    kwargs.update(overrides)
    return open_review_lane(**kwargs)


# --------------------------------------------------------------------------- #
# 1. materialize the lane at the PR head
# --------------------------------------------------------------------------- #
def test_open_materializes_lane_at_the_pr_head(review_world):
    rec = _open(review_world)
    lane = review_world["lane"]
    assert lane.exists()
    assert _run(lane, "rev-parse", "HEAD") == review_world["head_sha"]
    assert isinstance(rec, ReviewRecord)
    assert rec.head == review_world["head_sha"]
    assert rec.base == review_world["base_sha"]


# --------------------------------------------------------------------------- #
# 2. a wrong head REFUSES, before materializing, and leaves nothing behind
# --------------------------------------------------------------------------- #
def test_open_refuses_a_head_that_does_not_match_the_expected_sha(review_world):
    # GitHub said the head is base_sha; the source branch actually points at the
    # real head. The two disagree (a raced/tampered ref) -> refuse.
    with pytest.raises(ReviewError, match="head"):
        _open(review_world, expected_head_sha=review_world["base_sha"])
    assert not review_world["lane"].exists()
    assert not review_record_path(review_world["lane"]).exists()


def test_open_refusal_is_not_a_warning_the_record_is_never_written(review_world):
    with pytest.raises(ReviewError):
        _open(review_world, expected_head_sha="0" * 40)
    assert not review_record_path(review_world["lane"]).exists()


def test_open_refuses_a_wrong_head_BEFORE_materializing_anything(review_world, monkeypatch):
    """Negative fruit: a wrong head is refused fail-fast — the #807 clone seam is
    never even reached. Without this, a post-materialize cleanup could mask a
    removed pre-check (the guard-with-no-witness trap), leaving a wrong-head lane
    materialized and discarded on every mistaken open."""
    import gr2.python_cli.review as review_mod

    calls: list = []
    real = review_mod.ensure_lane_checkout

    def _spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(review_mod, "ensure_lane_checkout", _spy)
    with pytest.raises(ReviewError, match="head"):
        _open(review_world, expected_head_sha=review_world["base_sha"])
    assert calls == []  # materialization was never dispatched


# --------------------------------------------------------------------------- #
# 3. import resolution is printed at open (the PYTHONPATH trap)
# --------------------------------------------------------------------------- #
def test_open_prints_import_resolution_pointing_at_the_lane(review_world):
    lines: list[str] = []
    _open(review_world, echo=lines.append)
    joined = "\n".join(lines)
    assert "import resolution" in joined.lower()
    # names the lane, so a reviewer cannot mistake a machine-wide install for it
    assert str(review_world["lane"].resolve()) in joined


# --------------------------------------------------------------------------- #
# 4. cwd-containment asserted before any dispatched execution
# --------------------------------------------------------------------------- #
def test_cwd_containment_allows_the_lane_and_its_descendants(review_world):
    lane = review_world["lane"]
    _open(review_world)
    assert assert_cwd_contained(lane, lane) == lane.resolve()
    assert assert_cwd_contained(lane / "src", lane) == (lane / "src").resolve()


def test_cwd_containment_refuses_an_escaping_dispatch(review_world):
    lane = review_world["lane"]
    _open(review_world)
    with pytest.raises(ReviewError, match="escapes"):
        assert_cwd_contained(lane.parent, lane)  # an ancestor
    with pytest.raises(ReviewError, match="escapes"):
        assert_cwd_contained(review_world["source"], lane)  # a sibling (the base tree)


# --------------------------------------------------------------------------- #
# 4b. the gate fires on the real dispatch path, BEFORE the child spawns
# --------------------------------------------------------------------------- #
def test_run_in_review_lane_dispatches_inside_the_lane(review_world):
    lane = review_world["lane"]
    _open(review_world)
    marker = lane / "ran_here.txt"
    proc = run_in_review_lane(lane, ["git", "rev-parse", "--show-toplevel"], echo=lambda _l: None)
    assert proc.returncode == 0
    assert not marker.exists()  # sanity: command did not touch the source tree


def test_run_in_review_lane_refuses_an_escaping_cwd_before_spawning(review_world):
    lane = review_world["lane"]
    _open(review_world)
    # a child pointed at the base tree must be refused BEFORE it runs, so a
    # command that would create a file there never gets the chance
    escape_marker = review_world["source"] / "should_never_be_created.txt"
    with pytest.raises(ReviewError, match="escapes"):
        run_in_review_lane(
            lane,
            ["touch", str(escape_marker)],
            cwd=review_world["source"],
            echo=lambda _l: None,
        )
    assert not escape_marker.exists()


# --------------------------------------------------------------------------- #
# 5. the review record is EXACTLY the (repo, base, head) triple
# --------------------------------------------------------------------------- #
def test_review_record_is_the_pin_delta_triple_plus_lane_kind(review_world):
    # Per the gr2-lane-author-shape ruling (2026-09-03), a receipt carries the
    # (repo, base, head) triple PLUS a required lane_kind stamp so a reader never
    # infers the reconstruction guarantee. open_review_lane always materializes
    # an isolated clone, so its receipt is stamped "materialized".
    _open(review_world)
    data = json.loads(review_record_path(review_world["lane"]).read_text())
    assert set(data) == {"repo", "base", "head", "lane_kind"}  # nothing less, nothing more
    assert _HEX40.match(data["base"]) and _HEX40.match(data["head"])
    assert data["lane_kind"] == "materialized"
    # repo is derived from the source origin identity, not the lane working path
    origin = remote_origin_url(review_world["source"])
    assert data["repo"] == f"local:{Path(origin).resolve()}"
    assert str(review_world["lane"]) not in data["repo"]


def test_review_record_rejects_an_unknown_lane_kind():
    # lane_kind is a required, closed enum: a receipt that names a kind outside
    # {materialized, bound} is refused at construction, so no unrecognizable
    # guarantee can ever be written to disk.
    with pytest.raises(ReviewError, match="lane_kind must be one of"):
        ReviewRecord(repo="local:/x", base="a" * 40, head="b" * 40, lane_kind="quicklane")


def test_close_refuses_a_receipt_missing_lane_kind(review_world):
    """A receipt that would be a valid legacy triple but omits lane_kind is
    refused as not-well-formed — the required stamp cannot be silently absent.

    The lane is a real materialized clone (matching origin and HEAD), so the
    ONLY defect the close validator can be reacting to is the missing lane_kind:
    if the requirement were dropped, this record would pass the well-formed gate
    and close would proceed."""
    _open(review_world)
    lane = review_world["lane"]
    record = json.loads(review_record_path(lane).read_text())
    del record["lane_kind"]
    review_record_path(lane).write_text(json.dumps(record))
    with pytest.raises(ReviewError, match="not a well-formed"):
        close_review_lane(lane_repo_root=lane, review_lane_root=review_world["lane_root"])
    assert lane.exists()  # refused, not deleted


# --------------------------------------------------------------------------- #
# 6. close drops the lane + record; the base tree is untouched
# --------------------------------------------------------------------------- #
def test_close_drops_the_lane_and_record_and_leaves_the_source_intact(review_world):
    _open(review_world)
    lane = review_world["lane"]
    assert lane.exists()
    close_review_lane(lane_repo_root=lane, review_lane_root=review_world["lane_root"])
    assert not lane.exists()
    assert not review_record_path(lane).exists()
    # the base checkout and its PR ref survive
    assert review_world["source"].exists()
    ref_sha = _run(review_world["source"], "rev-parse", review_world["review_branch"])
    assert ref_sha == review_world["head_sha"]


# --------------------------------------------------------------------------- #
# 6b. the verbs are runtime-registered (walk the built registry, not decorators)
# --------------------------------------------------------------------------- #
def test_review_open_and_close_are_runtime_registered_verbs():
    import inspect

    from gr2.python_cli import app as app_mod

    by_name = {c.name: c for c in app_mod.review_app.registered_commands}
    assert {"open", "close"} <= set(by_name)
    for verb in ("open", "close"):
        cb = by_name[verb].callback
        assert callable(cb)
        params = inspect.signature(cb).parameters
        # the review-verb collapse: open/close dispatch on a
        # `target` argument, and both retain `pr_number` for the PR-head path.
        assert "target" in params and "pr_number" in params
    # open still takes the workspace_root (it always operates within a workspace);
    # close's target IS the thing to close (a reconstruction lane dir or a workspace).
    assert "workspace_root" in inspect.signature(by_name["open"].callback).parameters


# --------------------------------------------------------------------------- #
# 7. the lane is an INDEPENDENT clone, never a linked worktree
# --------------------------------------------------------------------------- #
def test_open_lane_is_an_independent_clone_not_a_worktree(review_world):
    _open(review_world)
    lane = review_world["lane"]
    git_dir = lane / ".git"
    assert git_dir.is_dir() and not git_dir.is_symlink()
    # reuse the #807 isolation verifier: it must not raise on the review lane
    clone_exec.verify_clone_isolation(
        lane,
        workspace_root=review_world["workspace_root"],
        repo_url=remote_origin_url(review_world["source"]),
        reference_base=None,
    )


# --------------------------------------------------------------------------- #
# P1-1. the expected head is bound from the HOST advertisement, not the fetch
# --------------------------------------------------------------------------- #
def test_host_pr_head_oid_reads_the_remote_advertisement(review_world):
    """host_pr_head_oid queries the remote's ref advertisement (ls-remote),
    which is the authority a fetch is checked against — not the fetched ref."""
    src = review_world["source"]
    # publish the head to origin as the PR ref (the object exists only in source)
    _run(src, "push", "-q", "origin", f"{review_world['review_branch']}:refs/pull/7/head")
    assert host_pr_head_oid(src, 7) == review_world["head_sha"]


def test_a_fetched_ref_that_disagrees_with_the_host_oid_refuses(review_world, monkeypatch):
    """If the local (fetched) ref does not equal the host-observed OID, open
    refuses before materializing — the compare is against an independent value,
    so a wrong/tampered fetch cannot pass by being compared to itself."""
    import gr2.python_cli.review as review_mod

    calls: list = []
    real = review_mod.ensure_lane_checkout
    monkeypatch.setattr(
        review_mod, "ensure_lane_checkout", lambda **k: (calls.append(k), real(**k))[1]
    )
    # host says the head is base_sha; the fetched review branch is at head_sha
    with pytest.raises(ReviewError, match="host advertises"):
        _open(review_world, expected_head_sha=review_world["base_sha"])
    assert calls == []


# --------------------------------------------------------------------------- #
# P1-2. a REUSED lane is preserved on a head mismatch (notes are NOT deleted)
# --------------------------------------------------------------------------- #
def test_reopen_at_a_moved_head_preserves_the_reused_lane_and_its_notes(review_world):
    _open(review_world)
    lane = review_world["lane"]
    note = lane / "REVIEWER_NOTES.txt"
    note.write_text("uncommitted review notes — must survive\n")

    # move the source PR branch to a new commit
    src = review_world["source"]
    _run(src, "checkout", "-q", review_world["review_branch"])
    (src / "more.txt").write_text("more\n")
    _run(src, "add", ".")
    _run(src, "commit", "-q", "-m", "moved head")
    h2 = _run(src, "rev-parse", "HEAD")
    _run(src, "checkout", "-q", "main")

    # reopen at the moved head: the reused lane is still at the old head, so it
    # is REFUSED and left byte-for-byte — the notes are the work this exists to protect
    with pytest.raises(ReviewError, match="preserved"):
        _open(review_world, expected_head_sha=h2)
    assert lane.exists()
    assert note.exists() and note.read_text() == "uncommitted review notes — must survive\n"


# --------------------------------------------------------------------------- #
# P1-3. close refuses any path that is not a review lane this tool opened
# --------------------------------------------------------------------------- #
def test_close_refuses_a_populated_non_lane_directory(tmp_path):
    # inside the managed root (so the SECONDARY gate is what fires), but no .git
    victim = tmp_path / "root" / "not-a-lane"
    victim.mkdir(parents=True)
    precious = victim / "precious.txt"
    precious.write_text("do not delete\n")
    with pytest.raises(ReviewError, match="lacks an owned .git"):
        close_review_lane(lane_repo_root=victim, review_lane_root=tmp_path / "root")
    assert victim.exists() and precious.exists() and precious.read_text() == "do not delete\n"


def test_close_refuses_a_git_repo_that_lacks_the_review_record(review_world, tmp_path):
    # a real clone INSIDE the managed root, but not one review-opened (no record)
    # -> the secondary consistency gate refuses, does not delete
    src = review_world["source"]
    root = tmp_path / "root"
    root.mkdir()
    clone = root / "bare-clone"
    _run(root, "clone", "--quiet", str(src), str(clone))
    keep = clone / "keep.txt"
    keep.write_text("keep\n")
    with pytest.raises(ReviewError, match="lacks an owned .git"):
        close_review_lane(lane_repo_root=clone, review_lane_root=root)
    assert clone.exists() and keep.exists()


# --------------------------------------------------------------------------- #
# P1-4. the repo identity is canonical and transport-independent
# --------------------------------------------------------------------------- #
def test_canonical_identity_is_transport_independent():
    canon = "https://github.com/synapt-dev/grip"
    assert canonical_source_identity("https://github.com/synapt-dev/grip.git") == canon
    assert canonical_source_identity("git@github.com:synapt-dev/grip.git") == canon
    assert canonical_source_identity("ssh://git@github.com/synapt-dev/grip.git") == canon
    assert canonical_source_identity("https://github.com/synapt-dev/grip") == canon
    # credentials, query, fragment stripped; owner/repo lowercased
    dirty = "https://x-token:abc@github.com/Synapt-Dev/Grip.git?a=1#f"
    assert canonical_source_identity(dirty) == canon


def test_canonical_identity_refuses_a_local_path_unless_allowed():
    with pytest.raises(ReviewError, match="not a canonical GitHub source"):
        canonical_source_identity("/some/local/bare.git")
    marked = canonical_source_identity("/some/local/bare.git", allow_local=True)
    assert marked.startswith("local:")
    assert "github.com" not in marked


# --------------------------------------------------------------------------- #
# P1-3b. the record must POSITIVELY identify the lane, not merely exist
# --------------------------------------------------------------------------- #
def test_close_refuses_a_clone_with_a_planted_empty_marker(review_world, tmp_path):
    """A `.git/grip-review.json` that merely EXISTS is not proof this tool opened
    the lane; an empty {} is refused, not treated as a review lane."""
    src = review_world["source"]
    root = tmp_path / "root"
    root.mkdir()
    clone = root / "planted"
    _run(root, "clone", "--quiet", str(src), str(clone))
    (clone / ".git" / "grip-review.json").write_text("{}\n")
    keep = clone / "keep.txt"
    keep.write_text("keep\n")
    with pytest.raises(ReviewError, match="not a well-formed"):
        close_review_lane(lane_repo_root=clone, review_lane_root=root)
    assert clone.exists() and keep.exists() and keep.read_text() == "keep\n"


def test_close_refuses_a_clone_whose_record_names_a_different_repo(review_world, tmp_path):
    """A well-formed record that describes a DIFFERENT repository is refused: the
    record must match the lane's own origin identity."""
    src = review_world["source"]
    root = tmp_path / "root"
    root.mkdir()
    clone = root / "mismatched"
    _run(root, "clone", "--quiet", str(src), str(clone))
    (clone / ".git" / "grip-review.json").write_text(
        json.dumps({"repo": "https://github.com/someone/else", "base": "a" * 40, "head": "b" * 40, "lane_kind": "materialized"})
    )
    keep = clone / "keep.txt"
    keep.write_text("keep\n")
    with pytest.raises(ReviewError, match="does not match"):
        close_review_lane(lane_repo_root=clone, review_lane_root=root)
    assert clone.exists() and keep.exists() and keep.read_text() == "keep\n"


def test_close_deletes_a_pristine_lane_inside_the_managed_root(review_world):
    """The positive case: a lane INSIDE the managed root whose record and HEAD
    both match its measured state is removed."""
    _open(review_world)
    lane = review_world["lane"]
    assert lane.exists() and review_record_path(lane).is_file()
    close_review_lane(lane_repo_root=lane, review_lane_root=review_world["lane_root"])
    assert not lane.exists()


# --------------------------------------------------------------------------- #
# v4 PRIMARY GATE — containment is the forgery-proof provenance, not the record
# --------------------------------------------------------------------------- #
def test_close_refuses_a_forged_record_outside_the_managed_root(review_world, tmp_path):
    """The core v4 finding: every record FIELD is attacker-suppliable. A
    same-origin clone OUTSIDE the review-lane root, carrying a FULLY
    self-consistent record — repo == its own canonical origin, head == its own
    HEAD, base a valid 40-hex — must SURVIVE close. Only the tool-owned
    containment boundary (path beneath the managed root) can distinguish this
    forged clone from a genuine lane; record matching alone cannot."""
    src = review_world["source"]
    managed_root = review_world["lane_root"]
    outside = tmp_path / "elsewhere" / "clone"
    outside.parent.mkdir(parents=True)
    _run(outside.parent, "clone", "--quiet", str(src), str(outside))
    own_head = _run(outside, "rev-parse", "HEAD")
    own_identity = canonical_source_identity(remote_origin_url(outside), allow_local=True)
    # a record that passes EVERY secondary check — repo, well-formed triple, HEAD
    review_record_path(outside).write_text(
        json.dumps({"repo": own_identity, "base": review_world["base_sha"], "head": own_head})
    )
    precious = outside / "reviewer_work.txt"
    precious.write_text("forged but real work — must survive\n")
    with pytest.raises(ReviewError, match="beneath the review-lane root"):
        close_review_lane(lane_repo_root=outside, review_lane_root=managed_root)
    assert outside.exists()
    assert precious.exists() and precious.read_text() == "forged but real work — must survive\n"


def test_close_refuses_the_managed_root_itself_so_siblings_survive(review_world, tmp_path):
    """v5 (Sentinel): containment must be STRICT descendant, not `beneath-or-equal`.
    Aiming close at the managed root ITSELF — even with a self-consistent record
    planted there — would recursively delete every SIBLING lane and the lane index
    along with it. The root is refused; the root, its planted record, a sibling
    lane, and the lane index all survive."""
    src = review_world["source"]
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    # plant a self-consistent lane AT the root (repo, well-formed triple, HEAD all pass)
    _run(managed_root.parent, "clone", "--quiet", str(src), str(managed_root / "_asrepo"))
    # make the root itself look like a lane clone: its own .git + record + HEAD
    _run(tmp_path, "clone", "--quiet", str(src), str(managed_root / "_tmp"))
    # move the clone's .git up so `managed_root` presents as an owned clone
    import shutil as _sh
    _sh.move(str(managed_root / "_tmp" / ".git"), str(managed_root / ".git"))
    _sh.rmtree(managed_root / "_tmp")
    _sh.rmtree(managed_root / "_asrepo")
    root_head = _run(managed_root, "rev-parse", "HEAD")
    root_identity = canonical_source_identity(remote_origin_url(managed_root), allow_local=True)
    review_record_path(managed_root).write_text(
        json.dumps({"repo": root_identity, "base": review_world["base_sha"], "head": root_head})
    )
    # a sibling lane and the lane index that a root-delete would destroy
    sibling = managed_root / "review-99" / "repos" / "grip"
    sibling.mkdir(parents=True)
    (sibling / "sibling_notes.txt").write_text("another reviewer's work\n")
    index = managed_root / "lane.toml"
    index.write_text("[lanes]\n")

    with pytest.raises(ReviewError, match="strictly beneath|root itself"):
        close_review_lane(lane_repo_root=managed_root, review_lane_root=managed_root)
    assert managed_root.exists()
    assert (sibling / "sibling_notes.txt").read_text() == "another reviewer's work\n"
    assert index.read_text() == "[lanes]\n"


def test_close_refuses_a_lane_inside_the_root_whose_head_has_moved(review_world):
    """Atlas's item, as a SECONDARY check inside the boundary: a genuine lane
    whose HEAD has moved off the recorded head (a reviewer commit) may hold work,
    so it is refused, not deleted as if pristine."""
    _open(review_world)
    lane = review_world["lane"]
    # a reviewer commits inside the lane: HEAD moves off the recorded review head
    (lane / "review_fix.txt").write_text("reviewer commit\n")
    _run(lane, "add", ".")
    _run(lane, "-c", "user.email=r@r", "-c", "user.name=r", "commit", "-q", "-m", "reviewer work")
    note = lane / "still_uncommitted.txt"
    note.write_text("also uncommitted\n")
    with pytest.raises(ReviewError, match="has moved|not the recorded"):
        close_review_lane(lane_repo_root=lane, review_lane_root=review_world["lane_root"])
    assert lane.exists() and note.exists()

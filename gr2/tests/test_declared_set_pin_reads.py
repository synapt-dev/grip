"""The declared-set pin tie reads a sibling's remote once, with no private ref: one ls-remote naming the target and
the review prefix, one fetch of the target by id, one fetch of every published review by id, read by id.

It was one ls-remote for the target and one fetch into a private transfer ref, then one ls-remote for the reviews and,
per review, one fetch into a private __merge_transfers__ ref and one update-ref -d. Counted at the process seam
(gitops._spawn + gitops.start); refs recorded after every git call the gate makes, not only before and after."""
from __future__ import annotations

import collections
import subprocess
import traceback

from gr2.python_cli import gitops, merge_gate
from tests.test_declared_set_pins import _move_beta, _refusal
from tests.test_declared_sets import SET, untouched, world  # noqa: F401  (fixtures)

_TRANSFERS = "refs/dev.synapt.grip/__merge_transfers__/"


def _refs(repo) -> set[str]:
    p = subprocess.run(["git", "for-each-ref", "--format=%(refname)"], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return set(p.stdout.split())


def test_an_unmerged_sibling_pin_is_read_with_no_private_ref_and_one_read_per_remote(world, monkeypatch):
    moved = _move_beta(world, push=True)  # beta's pin was pushed, nobody reviewed it: the tie scans beta's reviews
    beta = world["members"]["beta"]
    reviews = subprocess.run(
        ["git", "ls-remote", str(beta["remote"]), f"{merge_gate._REVIEW_REF_PREFIX}*"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    assert reviews, "the fixture publishes a review on beta's remote, so the scan has one to read"
    repos = [world["members"][n]["repo"] for n in SET]
    before = {str(r): _refs(r) for r in repos}
    seen: set[str] = set()  # every ref name any member repo held after any git call during the merge
    calls = collections.Counter()
    spawn, start = gitops._spawn, gitops.start

    def counted(argv, *a, **k):
        av = [str(x) for x in argv]
        verb = next((x for x in av[1:] if x in ("ls-remote", "fetch", "update-ref")), None)
        if verb:
            frames = [f for f in traceback.extract_stack() if f.filename.endswith("merge_gate.py")]
            if any(f.name == "tie" for f in frames):  # issued inside the pin tie, through any helper
                calls[(verb, "-d" in av)] += 1
        try:
            return spawn(argv, *a, **k)
        finally:
            for r in repos:
                seen.update(_refs(r))

    def started(cwd, *args, **kw):
        try:
            return start(cwd, *args, **kw)
        finally:
            for r in repos:
                seen.update(_refs(r))

    monkeypatch.setattr(gitops, "_spawn", counted)
    monkeypatch.setattr(gitops, "start", started)
    code, why, receipt = _refusal(world)
    monkeypatch.setattr(gitops, "_spawn", spawn)
    monkeypatch.setattr(gitops, "start", start)

    # the outcome and receipt are the ones the temp-ref read gave
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why == (f"declared_set_pin_unreviewed: beta is pinned at {moved[:12]}, which is neither merged into main "
                   f"nor named by a review published on its remote"), why
    assert untouched(world)
    # no private ref, ever: not after the run, not between any two git calls
    assert not [r for r in seen if r.startswith(_TRANSFERS)], sorted(seen)
    assert {str(r): _refs(r) for r in repos} == before
    # beta's remote: one ls-remote (target + review prefix), one fetch of the target by id, one of the reviews by id
    assert dict(calls) == {("ls-remote", False): 1, ("fetch", False): 2}, dict(calls)

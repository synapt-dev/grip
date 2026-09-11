"""`gr2 review rebind` contract.

A frozen public-push range's `behind 0` is a moment property — the integration
branch moves under it constantly, and today every such death is a hand re-freeze
plus two fresh reads. `review rebind` rebases the frozen range onto the moved base
WHEN the moving commit is unrelated (patch-ids identical, rename-aware net diff
identical) and REFUSES the moment the range would actually change.

These tests pin the contract on real git fixtures before the implementation exists.
The core outcomes:
  * base unchanged        -> no-op, "still valid"
  * unrelated base move   -> rebased, patch-ids identical, new frozen dir emitted
  * range would change    -> REFUSE (a real conflict or a patch-id divergence)
  * range already landed  -> "already applied", names the landing commit
  * intended ref public   -> REFUSE (a force-push question, not a rebase)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from python_cli import review_rebind as rb


def _git(r: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(r), *a], text=True, capture_output=True, check=True
    ).stdout.strip()


def _origin_with_base(tmp_path: Path) -> tuple[Path, str]:
    """A bare-ish origin on branch dev with one commit; returns (origin, dev-sha)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "dev")
    _git(origin, "config", "user.email", "dev@layne.pro")
    _git(origin, "config", "user.name", "Layne Penney")
    (origin / "a.txt").write_text("one\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "c0", "--no-gpg-sign")
    return origin, _git(origin, "rev-parse", "HEAD")


def _frozen_range(tmp_path: Path, origin: Path, base_sha: str) -> Path:
    """Freeze a one-commit range (edit b.txt) against dev, into a frozen dir.

    Uses the real freeze via a clone so REQUEST.md/range.patch match production shape.
    """
    clone = tmp_path / "author"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "dev@layne.pro")
    _git(clone, "config", "user.name", "Layne Penney")
    _git(clone, "checkout", "-q", "-b", "feat/x")
    (clone / "b.txt").write_text("feature\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "add b", "--no-gpg-sign")
    out = tmp_path / "frozen-v1"
    rb.freeze(clone, "refs/remotes/origin/dev", out,
              title="feat: add b", body="Body.\n\nPremium boundary: grip is OSS.\n")
    return out


def _move_base_unrelated(origin: Path) -> str:
    """Land an UNRELATED commit on dev (touches a different file); returns new sha."""
    _git(origin, "checkout", "-q", "dev")
    (origin / "unrelated.txt").write_text("moved\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "unrelated move", "--no-gpg-sign")
    return _git(origin, "rev-parse", "HEAD")


def test_base_unchanged_is_noop(tmp_path):
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    author = tmp_path / "author"
    res = rb.rebind(frozen, author, "refs/remotes/origin/dev", tmp_path / "frozen-v2")
    assert res.outcome == "base_unchanged"
    assert not (tmp_path / "frozen-v2").exists()


def test_unrelated_base_move_rebases_patch_id_identical(tmp_path, monkeypatch):
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    _move_base_unrelated(origin)
    author = tmp_path / "author"
    _git(author, "fetch", "-q", "origin")
    out = tmp_path / "frozen-v2"
    # rebind's internal `git am` writes a commit, so it needs a committer identity.
    # A CI runner has no global git config AND a domainless hostname, so git's
    # auto-detected email is rejected there; rebind must set its own identity on the
    # clone. Reproduce the no-global-config half here (GIT_CONFIG_GLOBAL=/dev/null,
    # GIT_CONFIG_NOSYSTEM=1, identity env unset) so this run does not silently depend
    # on the developer host's ~/.gitconfig. The hostname half is not reproducible off
    # a runner, so a green here does not fully witness the fix — CI on the pushed head
    # is the only complete witness (Stromus + Apollo, 2026-09-11).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    res = rb.rebind(frozen, author, "refs/remotes/origin/dev", out)
    assert res.outcome == "rebased"
    assert res.patch_id_held is True
    assert out.is_dir()
    # title/body NORM unchanged; range.patch moved (base sha in header changed)
    assert (out / "range.patch").read_text() != (frozen / "range.patch").read_text()


def test_range_already_applied_reports_landing(tmp_path):
    """The base moves by landing the SAME work the frozen range adds (someone merged
    it): the range is already contained in the moved base, so rebind reports
    already_applied and names the landing commit rather than refusing as a
    patch-id divergence (empty rebased range)."""
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    # land the identical change (b.txt=feature) directly on dev
    _git(origin, "checkout", "-q", "dev")
    (origin / "b.txt").write_text("feature\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "add b", "--no-gpg-sign")
    landing = _git(origin, "rev-parse", "HEAD")
    author = tmp_path / "author"
    _git(author, "fetch", "-q", "origin")
    res = rb.rebind(frozen, author, "refs/remotes/origin/dev", tmp_path / "frozen-v2")
    assert res.outcome == "already_applied"
    assert res.landing_sha == landing
    assert not (tmp_path / "frozen-v2").exists()


def test_slash_bearing_target_ref_resolves(tmp_path):
    """A slash-bearing target ref (release/1.x) must ls-remote the whole branch name,
    not just the last path segment. rsplit('/',1)[-1] gave '1.x' -> refs/heads/1.x
    absent -> no_live_base; the whole name resolves and, base unchanged, gives
    base_unchanged."""
    origin, base = _origin_with_base(tmp_path)
    _git(origin, "branch", "release/1.x", base)  # a slash-bearing branch on origin
    clone = tmp_path / "author"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "dev@layne.pro")
    _git(clone, "config", "user.name", "Layne Penney")
    _git(clone, "checkout", "-q", "-b", "feat/y", "origin/release/1.x")
    (clone / "c.txt").write_text("feature\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "add c", "--no-gpg-sign")
    out = tmp_path / "frozen-r1x"
    rb.freeze(clone, "refs/remotes/origin/release/1.x", out,
              title="feat: add c", body="Body.\n\nPremium boundary: grip is OSS.\n")
    res = rb.rebind(out, clone, "refs/remotes/origin/release/1.x", tmp_path / "frozen-r1x-v2")
    assert res.outcome == "base_unchanged"


def test_intended_ref_public_refuses(tmp_path):
    """The intended ref is already on the public remote: a rebind would produce a new
    head, so it is a force-push question, not a rebase. REFUSE (mirrors the freeze
    script's ref-absent assertion / exit 6)."""
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    author = tmp_path / "author"
    _git(author, "push", "-q", "origin", "feat/x")  # intended ref now public
    with pytest.raises(rb.RebindRefused) as ei:
        rb.rebind(frozen, author, "refs/remotes/origin/dev", tmp_path / "frozen-v2")
    assert ei.value.code == "intended_ref_public"
    assert not (tmp_path / "frozen-v2").exists()


def test_intended_ref_public_allowed_with_flag(tmp_path):
    """--allow-public-ref is the sanctioned fix-forward on an already-pushed ref."""
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    author = tmp_path / "author"
    _git(author, "push", "-q", "origin", "feat/x")
    res = rb.rebind(frozen, author, "refs/remotes/origin/dev", tmp_path / "frozen-v2",
                    allow_public_ref=True)
    assert res.outcome == "base_unchanged"


def test_range_that_would_change_refuses(tmp_path):
    """A base move that edits the SAME line the range touches makes the rebased range
    differ -> REFUSE, not a silent rebind."""
    origin, base = _origin_with_base(tmp_path)
    frozen = _frozen_range(tmp_path, origin, base)
    # move dev by editing b.txt itself (collides with the frozen range's file)
    _git(origin, "checkout", "-q", "dev")
    (origin / "b.txt").write_text("conflicting\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "touch b", "--no-gpg-sign")
    author = tmp_path / "author"
    _git(author, "fetch", "-q", "origin")
    with pytest.raises(rb.RebindRefused):
        rb.rebind(frozen, author, "refs/remotes/origin/dev", tmp_path / "frozen-v2")

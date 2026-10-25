"""A plain `git clone` of a full-mode root: gr2 checkout and gr2 pull fetch the gr2 names once; status and commit
never touch the network and name gr2 checkout as the step."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import griplink

from tests.test_root_push_default import git, gr2, no_trace, refs, world  # noqa: F401 (world is a fixture)


def _names(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/dev.synapt.grip/")


def _pushed_clone(world, monkeypatch, name: str = "plain") -> Path:
    assert gr2(world["root"], monkeypatch, "push").exit_code == 0
    clone = world["tmp"] / name
    git(world["tmp"], "clone", "-q", str(world["origin"]), str(clone))
    assert _names(clone) == ""
    return clone


def test_r1_clone_checkout_then_status_with_no_hand_fetch(world, monkeypatch):
    clone = _pushed_clone(world, monkeypatch)
    r = gr2(clone, monkeypatch, "checkout")
    no_trace(r)
    assert r.exit_code == 0, r.stderr
    assert _names(clone) == _names(world["origin"])
    r = gr2(clone, monkeypatch, "status")
    no_trace(r)
    assert r.exit_code == 0, r.stderr


def test_r2_clone_then_pull_fetches_the_names(world, monkeypatch):
    clone = _pushed_clone(world, monkeypatch)
    r = gr2(clone, monkeypatch, "pull")
    no_trace(r)
    assert r.exit_code == 0, r.stderr
    assert git(clone, "rev-parse", griplink.STOOK) == git(world["origin"], "rev-parse", griplink.STOOK)


def test_r3_status_and_commit_before_checkout_refuse_naming_checkout_and_fetch_nothing(world, monkeypatch):
    clone = _pushed_clone(world, monkeypatch)
    for verb in ("status", "commit"):
        args = [verb] if verb == "status" else [verb, "-m", "x"]
        r = gr2(clone, monkeypatch, *args)
        no_trace(r)
        assert r.exit_code == 4, (verb, r.stdout, r.stderr)
        assert r.stderr.strip() == (f"gr2 {verb}: this workspace's names were not fetched (a plain git clone has main "
                                    f"only); run gr2 checkout (or gr2 pull), then gr2 {verb} again")
        assert "git fetch" not in r.stderr
        assert _names(clone) == ""


def test_r4_a_remote_with_no_gr2_names_refuses_pull_at_4(world, monkeypatch):
    assert gr2(world["root"], monkeypatch, "push").exit_code == 0
    bare = world["tmp"] / "nonames.git"
    git(world["tmp"], "clone", "-q", "--bare", str(world["origin"]), str(bare))
    assert _names(bare) == ""
    clone = world["tmp"] / "plain"
    git(world["tmp"], "clone", "-q", str(bare), str(clone))
    r = gr2(clone, monkeypatch, "pull")
    no_trace(r)
    assert r.exit_code == 4, (r.stdout, r.stderr)
    assert r.stderr.strip() == (f"gr2 pull: the remote origin has no gr2 names yet (no {griplink.STOOK}); nothing "
                                "was fetched")
    assert _names(clone) == ""


def test_r5_names_are_fetched_once_then_no_remote_is_needed(world, monkeypatch):
    clone = _pushed_clone(world, monkeypatch)
    assert gr2(clone, monkeypatch, "checkout").exit_code == 0
    world["origin"].rename(world["tmp"] / "moved.git")
    for verb in ("status", "checkout"):
        r = gr2(clone, monkeypatch, verb)
        no_trace(r)
        assert r.exit_code == 0, (verb, r.stderr)


def test_r6_a_clone_of_a_remote_whose_head_names_master_checks_out_main(world, monkeypatch):
    assert gr2(world["root"], monkeypatch, "push").exit_code == 0
    git(world["origin"], "symbolic-ref", "HEAD", "refs/heads/master")
    clone = world["tmp"] / "plain"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(clone)], check=True, capture_output=True)
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True).returncode != 0
    r = gr2(clone, monkeypatch, "checkout")
    no_trace(r)
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "ambiguous argument" not in r.stderr
    assert git(clone, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(clone, "rev-parse", "HEAD") == git(world["origin"], "rev-parse", "refs/heads/main")
    assert gr2(clone, monkeypatch, "status").exit_code == 0


def test_r7_a_local_name_that_differs_survives_the_names_fetch(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    assert gr2(root, monkeypatch, "push").exit_code == 0
    remote_sha = git(root, "rev-parse", "HEAD")
    git(root, "push", "-q", str(origin), f"{remote_sha}:refs/dev.synapt.grip/x-note")
    clone = world["tmp"] / "plain"
    git(world["tmp"], "clone", "-q", str(origin), str(clone))
    git(clone, "commit", "-q", "--allow-empty", "-m", "local only")
    local_sha = git(clone, "rev-parse", "HEAD")
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    git(clone, "update-ref", "refs/dev.synapt.grip/x-note", local_sha)
    r = gr2(clone, monkeypatch, "pull")
    no_trace(r)
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert git(clone, "rev-parse", "refs/dev.synapt.grip/x-note") == local_sha
    assert git(clone, "rev-parse", griplink.STOOK) == git(origin, "rev-parse", griplink.STOOK)


def test_r8_a_tail_matching_name_outside_the_gr2_namespace_is_not_fetched(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    assert gr2(root, monkeypatch, "push").exit_code == 0
    git(root, "push", "-q", str(origin), f"HEAD:refs/a/{griplink.STOOK}")
    clone = world["tmp"] / "plain"
    git(world["tmp"], "clone", "-q", str(origin), str(clone))
    r = gr2(clone, monkeypatch, "pull")
    no_trace(r)
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert git(clone, "for-each-ref", "refs/a") == ""
    assert _names(clone) == _names(origin)


def _no_names_clone(world, monkeypatch) -> tuple[Path, Path]:
    assert gr2(world["root"], monkeypatch, "push").exit_code == 0
    bare = world["tmp"] / "nonames.git"
    git(world["tmp"], "clone", "-q", "--bare", str(world["origin"]), str(bare))
    clone = world["tmp"] / "plain"
    git(world["tmp"], "clone", "-q", str(bare), str(clone))
    assert _names(bare) == "" and _names(clone) == ""
    return bare, clone


def test_r9_checkout_of_a_clone_of_a_remote_with_no_gr2_names_runs_as_before(world, monkeypatch):
    _, clone = _no_names_clone(world, monkeypatch)
    r = gr2(clone, monkeypatch, "checkout")
    no_trace(r)
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "api: at its pin" in r.stdout
    assert _names(clone) == ""


def test_r10_push_of_a_clone_of_a_remote_with_no_gr2_names_refuses_as_before(world, monkeypatch):
    bare, clone = _no_names_clone(world, monkeypatch)
    before = git(bare, "for-each-ref")
    r = gr2(clone, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 4, (r.stdout, r.stderr)
    assert r.stderr.startswith("gr2 push: this workspace's names were not fetched"), r.stderr
    assert git(bare, "for-each-ref") == before
    assert _names(clone) == ""


def test_r11_a_remote_name_holding_a_line_separator_does_not_split_the_listing(world, monkeypatch):
    root, origin = world["root"], world["origin"]
    assert gr2(root, monkeypatch, "push").exit_code == 0
    git(root, "push", "-q", str(origin), f"HEAD:{griplink.NS}/a\u2028b")
    clone = world["tmp"] / "plain"
    git(world["tmp"], "clone", "-q", str(origin), str(clone))
    r = gr2(clone, monkeypatch, "pull")
    no_trace(r)
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert _names(clone) == _names(origin)
    assert griplink.STOOK in _names(clone)


def _unreachable(world, clone: Path) -> None:
    git(clone, "remote", "set-url", "origin", str(world["tmp"] / "gone.git"))


def test_r12_checkout_with_an_unreachable_remote_runs_as_before(world, monkeypatch):
    _, bare_clone = _no_names_clone(world, monkeypatch)
    named_clone = world["tmp"] / "named"
    git(world["tmp"], "clone", "-q", str(world["origin"]), str(named_clone))
    for clone in (bare_clone, named_clone):
        _unreachable(world, clone)
        r = gr2(clone, monkeypatch, "checkout")
        no_trace(r)
        assert r.exit_code == 0, (clone.name, r.stdout, r.stderr)
        assert "api: at its pin" in r.stdout
        assert _names(clone) == ""


def test_r13_push_with_an_unreachable_remote_refuses_as_before(world, monkeypatch):
    bare, clone = _no_names_clone(world, monkeypatch)
    before = git(bare, "for-each-ref")
    _unreachable(world, clone)
    r = gr2(clone, monkeypatch, "push")
    no_trace(r)
    assert r.exit_code == 4, (r.stdout, r.stderr)
    assert r.stderr.startswith("gr2 push: this workspace's names were not fetched"), r.stderr
    assert git(bare, "for-each-ref") == before
    assert _names(clone) == ""

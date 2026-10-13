"""`store materialize` leaves each member ON A BRANCH at its pin, not on a detached HEAD.

Measured on the Show HN store run (machine two): after `git clone <root>` and `store
materialize`, every member sat on `HEAD (no branch)` at its pin, and someone continuing the
work had to make or switch to a branch first, per member, with nothing in the output saying
so. The Git arm of the same trial ended each member on `main`, one ahead of origin.

So materialize puts each member on the branch its `upstream` names (`origin/main` -> local
`main`), at the pin, tracking that upstream. It moves that branch only when doing so drops no
commit that exists only here: `git rev-list <branch> --not <pin> --remotes` must be empty. A
branch carrying commits no remote holds is left alone, the member stays detached at the pin,
and materialize says so, by name, with the command to continue.

Premium boundary: OSS (grip). Local workspace materialization over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_store_full_mode_layers import _fresh_clone_without_the_pin, _layered_store
from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _branch(repo: Path) -> str | None:
    head = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    return head.stdout.strip() if head.returncode == 0 else None


def _tracking(repo: Path, branch: str) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}", check=False).stdout.strip()


def _compat_root_and_fresh_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Two members, a root commit, the root pushed, and a fresh root clone with no member checkouts."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0
    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-q", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "-q", "--no-checkout", str(root_remote), str(fresh))
    return root, fresh


def test_a_fresh_materialize_lands_every_member_on_its_upstream_branch_at_the_pin(tmp_path: Path) -> None:
    root, fresh = _compat_root_and_fresh_clone(tmp_path)

    done = gr2(fresh, "materialize", "--json")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    rows = {row["name"]: row for row in json.loads(done.stdout.splitlines()[0])["members"]}
    for name in ("alpha", "beta"):
        pin = git(root / name, "rev-parse", "HEAD").stdout.strip()
        assert git(fresh / name, "rev-parse", "HEAD").stdout.strip() == pin
        assert _branch(fresh / name) == "refs/heads/main", f"{name} is detached"
        assert _tracking(fresh / name, "main") == "origin/main"
        assert rows[name]["branch"] == "main" and "detached" not in rows[name], rows[name]


def test_a_recorded_branch_that_is_not_the_clone_default_is_created_and_tracks_its_upstream(tmp_path: Path) -> None:
    """A fresh clone checks out the remote's default branch, so a member recorded on another branch has no local
    branch of that name: materialize creates it AND sets its tracking, which nothing else would. The branch name is
    the member's recorded `ref`, the tracking its `upstream`: alpha is on `dev` tracking `origin/dev`, beta on a
    local `mine` tracking `origin/main`."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    git(root / "alpha", "checkout", "-q", "-b", "dev")
    (root / "alpha" / "dev.txt").write_text("dev\n")
    git(root / "alpha", "add", "dev.txt")
    git(root / "alpha", "commit", "-q", "-m", "on dev")
    git(root / "alpha", "push", "-q", "-u", "origin", "dev")
    git(root / "beta", "checkout", "-q", "-b", "mine", "--track", "origin/main")
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    spec = (root / "grip.toml").read_text()
    assert 'ref = "dev"' in spec and 'upstream = "origin/dev"' in spec, spec  # the fixture's precondition
    assert 'ref = "mine"' in spec and 'upstream = "origin/main"' in spec, spec
    assert gr2(root, "commit", "-m", "first").returncode == 0
    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-q", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "-q", "--no-checkout", str(root_remote), str(fresh))

    done = gr2(fresh, "materialize")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert _branch(fresh / "alpha") == "refs/heads/dev"
    assert _tracking(fresh / "alpha", "dev") == "origin/dev"
    assert _branch(fresh / "beta") == "refs/heads/mine"
    assert _tracking(fresh / "beta", "mine") == "origin/main"
    for name in ("alpha", "beta"):
        assert git(fresh / name, "rev-parse", "HEAD").stdout.strip() == git(root / name, "rev-parse", "HEAD").stdout.strip()


def test_a_pin_behind_the_upstream_tip_still_lands_on_the_branch(tmp_path: Path) -> None:
    """The member moved on (and pushed) after the root recorded it: the fresh clone's `main` starts at the
    upstream tip, past the pin. Moving it back to the pin drops nothing, because every commit is on origin."""
    root, fresh = _compat_root_and_fresh_clone(tmp_path)
    pin = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    (root / "alpha" / "later.txt").write_text("later\n")
    git(root / "alpha", "add", "later.txt")
    git(root / "alpha", "commit", "-q", "-m", "later")
    git(root / "alpha", "push", "-q", "origin", "main")
    tip = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    assert tip != pin, "the upstream must have moved past the pin for this row to discriminate"

    done = gr2(fresh, "materialize")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin
    assert _branch(fresh / "alpha") == "refs/heads/main"
    assert git(fresh / "alpha", "rev-parse", "origin/main").stdout.strip() == tip


def test_a_layered_pin_lands_on_the_branch_one_ahead_of_origin(tmp_path: Path) -> None:
    """The measured case: a work-in-progress pin no remote holds, carried as a layer. The Git arm ended on
    `main`, one ahead of origin; materialize now does the same."""
    root, pin = _layered_store(tmp_path)
    fresh = _fresh_clone_without_the_pin(tmp_path, root, pin)

    done = gr2(fresh, "materialize")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin
    assert _branch(fresh / "alpha") == "refs/heads/main"
    counts = git(fresh / "alpha", "rev-list", "--left-right", "--count", "main...origin/main").stdout.split()
    assert counts == ["1", "0"], counts


def test_a_branch_with_commits_no_remote_holds_is_left_alone_and_named(tmp_path: Path) -> None:
    """An adopted checkout whose `main` carries a local-only commit: moving `main` to the pin would drop it.
    So `main` is untouched, the member is detached at the pin, and materialize says so with the way on."""
    root, fresh = _compat_root_and_fresh_clone(tmp_path)
    pin = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    url = git(root / "alpha", "remote", "get-url", "origin").stdout.strip()
    git(fresh, "clone", "-q", url, "alpha")
    configure_identity(fresh / "alpha")
    (fresh / "alpha" / "local.txt").write_text("only here\n")
    git(fresh / "alpha", "add", "local.txt")
    git(fresh / "alpha", "commit", "-q", "-m", "only here")
    local_only = git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip()

    done = gr2(fresh, "materialize", "--json")

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh / "alpha", "rev-parse", "refs/heads/main").stdout.strip() == local_only, "main was moved"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin
    assert _branch(fresh / "alpha") is None
    rows = {row["name"]: row for row in json.loads(done.stdout.splitlines()[0])["members"]}
    assert rows["alpha"]["branch"] is None and "no remote holds" in rows["alpha"]["detached"], rows["alpha"]
    assert rows["beta"]["branch"] == "main", "the other member still lands on its branch (control)"

    text = gr2(fresh, "materialize")
    assert text.returncode == 0
    assert "alpha: left detached at" in text.stderr and "git -C" in text.stderr, text.stderr

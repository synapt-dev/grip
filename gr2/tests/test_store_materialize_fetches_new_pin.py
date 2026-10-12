"""`store materialize` on a member that already exists FETCHES a pin it does not hold yet.

Measured in the review cell (three workspaces, five members on local bare remotes): after one workspace
merged into demo-core and ran `store commit` + `store push`, a second workspace pulled the root (its first
pull since the root gained a .gitmodules, so git fetched no member on the side) and ran
`store materialize`, which refused with `demo-core pin <merge> cannot be served by origin` at exit 3
while origin's main WAS that merge. The existing clone had simply never fetched it; one hand
`git fetch` per member made the same command exit 0. So the verb that exists to bring a workspace to the
root's pins could not follow a pin another workspace recorded, and its message blamed origin.

Contract:
- a pin the existing clone lacks is fetched from origin before the checkout, and materialize exits 0 with
  the member on its upstream branch at the new pin (the reader's probe, end to end);
- a pin origin really does not have still refuses at exit 3 naming the member and the pin, and only after
  a fetch that answered without it; a clone that already holds its pin still materializes with every remote
  unreachable.

Premium boundary: OSS (grip). Local workspace materialization over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, run
from tests.test_store_materialize_branch import _branch, _compat_root_and_fresh_clone


def _second_workspace_records_a_new_alpha_pin(root: Path) -> str:
    """The first workspace commits in alpha, pushes it, records the new pin in the root, and pushes the root."""
    (root / "alpha" / "next.txt").write_text("next\n")
    git(root / "alpha", "add", "next.txt")
    git(root / "alpha", "commit", "-q", "-m", "next")
    git(root / "alpha", "push", "-q", "origin", "HEAD:main")
    assert gr2(root, "commit", "-m", "alpha moved").returncode == 0
    git(root, "push", "-q", "origin", "HEAD:main")
    return git(root / "alpha", "rev-parse", "HEAD").stdout.strip()


def _reader_clone(tmp_path: Path) -> tuple[Path, Path]:
    """The root and a second workspace cloned the way a reader clones it: a plain `git clone` of the root."""
    root, _ = _compat_root_and_fresh_clone(tmp_path)
    second = tmp_path / "second"
    run(tmp_path, "git", "clone", "-q", str(tmp_path / "root.git"), str(second))
    return root, second


def _pull_root_without_member_fetch(workspace: Path) -> None:
    """Pull the root the way the review cell's first pulls did: git does not fetch the members on the side.
    git's on-demand submodule fetch can fetch a changed member during a root pull, but not on a first pull from a
    root whose checkout had no .gitmodules yet (the tutorial's shape: its main has none until the first store
    commit), and never with fetch.recurseSubmodules=false. materialize must not depend on it."""
    git(workspace, "-c", "fetch.recurseSubmodules=false", "pull", "-q", "--ff-only", "origin", "main")


def _materialized_once(fresh: Path) -> None:
    done = gr2(fresh, "materialize", "--json")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    configure_identity(fresh)


def test_an_existing_member_follows_a_pin_another_workspace_recorded(tmp_path: Path) -> None:
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    new_pin = _second_workspace_records_a_new_alpha_pin(root)
    _pull_root_without_member_fetch(fresh)
    assert git(fresh / "alpha", "cat-file", "-e", f"{new_pin}^{{commit}}", check=False).returncode != 0, \
        "precondition: the second workspace's alpha clone must not hold the new pin yet"

    done = gr2(fresh, "materialize", "--json", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == new_pin
    assert _branch(fresh / "alpha") == "refs/heads/main"
    rows = {row["name"]: row for row in json.loads(done.stdout.splitlines()[0])["members"]}
    assert rows["alpha"]["pin"] == new_pin and rows["alpha"]["head"] == new_pin, rows["alpha"]


def test_a_pin_origin_does_not_have_still_refuses_naming_the_member_and_pin(tmp_path: Path) -> None:
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    _second_workspace_records_a_new_alpha_pin(root)
    _pull_root_without_member_fetch(fresh)
    # The pin's commit leaves origin: main is reset to its parent, and the bare remote drops the object.
    alpha_remote = tmp_path / "alpha.git"
    pin = git(fresh, "ls-tree", "HEAD", "alpha").stdout.split()[2]
    git(alpha_remote, "update-ref", "refs/heads/main", f"{pin}^")
    git(alpha_remote, "reflog", "expire", "--expire=now", "--all")
    git(alpha_remote, "gc", "-q", "--prune=now")
    assert git(alpha_remote, "cat-file", "-e", f"{pin}^{{commit}}", check=False).returncode != 0, \
        "precondition: origin must not hold the pin"

    done = gr2(fresh, "materialize", check=False)

    assert done.returncode == 3, f"{done.stdout}\n{done.stderr}"
    assert "alpha" in done.stderr + done.stdout and pin in done.stderr + done.stdout


def test_a_clone_that_already_holds_its_pin_materializes_with_every_remote_unreachable(tmp_path: Path) -> None:
    """With every remote unreachable, a clone already holding its pin still materializes at exit 0."""
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    for name in ("alpha", "beta"):
        git(fresh / name, "remote", "set-url", "origin", str(tmp_path / "gone" / f"{name}.git"))

    done = gr2(fresh, "materialize", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"


def test_a_clone_that_already_holds_its_pin_asks_origin_nothing(tmp_path: Path) -> None:
    """The fetch is for a missing pin only. origin's upload-pack is wrapped to log each call; a clone holding its pin
    logs none, and the same wrapper logs once a pin goes missing, so the witness can fire."""
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    log = tmp_path / "upload-pack.log"
    wrapper = tmp_path / "upload-pack-logged"
    wrapper.write_text(f'#!/bin/sh\necho called >> "{log}"\nexec git-upload-pack "$@"\n')
    wrapper.chmod(0o755)
    git(fresh / "alpha", "config", "remote.origin.uploadpack", str(wrapper))

    assert gr2(fresh, "materialize", check=False).returncode == 0
    assert not log.exists(), "materialize asked origin for a pin the clone already holds"

    new_pin = _second_workspace_records_a_new_alpha_pin(root)
    _pull_root_without_member_fetch(fresh)
    assert gr2(fresh, "materialize", check=False).returncode == 0
    assert log.exists(), "control: the wrapper never logged, so its silence above proved nothing"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == new_pin


def test_a_member_clone_with_a_narrowed_refspec_still_gets_the_pin(tmp_path: Path) -> None:
    """A --single-branch clone fetches one branch only, so a plain fetch never brings a pin recorded on another
    branch. materialize asks origin for the pin itself."""
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    git(tmp_path / "alpha.git", "branch", "side", "main")
    # The shape `git clone --single-branch -b side` leaves: one refspec, and the branch's upstream on that branch too
    # (a plain `git fetch` also fetches the current branch's upstream into FETCH_HEAD).
    git(fresh / "alpha", "config", "--replace-all", "remote.origin.fetch", "+refs/heads/side:refs/remotes/origin/side")
    git(fresh / "alpha", "config", "branch.main.merge", "refs/heads/side")
    new_pin = _second_workspace_records_a_new_alpha_pin(root)
    _pull_root_without_member_fetch(fresh)
    git(fresh / "alpha", "fetch", "-q", "origin")
    assert git(fresh / "alpha", "cat-file", "-e", f"{new_pin}^{{commit}}", check=False).returncode != 0, \
        "precondition: a plain fetch with this refspec does not bring the pin"

    done = gr2(fresh, "materialize", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == new_pin


def test_an_unreachable_origin_is_named_and_never_reads_as_cannot_be_served(tmp_path: Path) -> None:
    """When the fetch cannot reach origin, the refusal says origin could not be asked, with git's reason; it never
    says origin cannot serve the pin, which nobody measured."""
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    new_pin = _second_workspace_records_a_new_alpha_pin(root)
    _pull_root_without_member_fetch(fresh)
    git(fresh / "alpha", "remote", "set-url", "origin", str(tmp_path / "gone" / "alpha.git"))

    done = gr2(fresh, "materialize", check=False)

    said = done.stdout + done.stderr
    assert done.returncode == 3, said
    assert f"alpha pin {new_pin} is not in the clone and origin could not be asked" in said, said
    assert "cannot be served by origin" not in said, said


def test_a_remote_that_refuses_a_fetch_by_sha_still_serves_a_pin_behind_a_tip(tmp_path: Path) -> None:
    """Some servers refuse a commit asked for by sha when no ref names it. materialize asks for origin's branches,
    never a sha, so a pin that is no longer a branch tip still arrives from such a server."""
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    new_pin = _second_workspace_records_a_new_alpha_pin(root)
    (root / "alpha" / "later.txt").write_text("later\n")   # the pin stops being main's tip on origin
    git(root / "alpha", "add", "later.txt")
    git(root / "alpha", "commit", "-q", "-m", "later")
    git(root / "alpha", "push", "-q", "origin", "HEAD:main")
    _pull_root_without_member_fetch(fresh)
    git(fresh / "alpha", "config", "protocol.version", "0")   # v0 refuses an unadvertised sha by default
    probe = tmp_path / "probe"
    run(tmp_path, "git", "init", "-q", str(probe))
    refused = git(probe, "-c", "protocol.version=0", "fetch", "-q", str(tmp_path / "alpha.git"), new_pin, check=False)
    assert refused.returncode != 0, "precondition: origin must refuse this pin when asked for it by sha"

    done = gr2(fresh, "materialize", check=False)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == new_pin


def test_a_pin_origin_holds_but_no_branch_reaches_still_refuses(tmp_path: Path) -> None:
    """An upstream force-pushed over the pin leaves the commit in origin's object store with no branch reaching it.
    Asked for by sha, origin would hand it over; it is not served, so materialize refuses as before."""
    root, fresh = _reader_clone(tmp_path)
    _materialized_once(fresh)
    new_pin = _second_workspace_records_a_new_alpha_pin(root)
    _pull_root_without_member_fetch(fresh)
    alpha_remote = tmp_path / "alpha.git"
    git(alpha_remote, "update-ref", "refs/heads/main", f"{new_pin}^")   # the force-push: main no longer reaches it
    assert git(alpha_remote, "cat-file", "-e", f"{new_pin}^{{commit}}", check=False).returncode == 0, \
        "precondition: origin still holds the object"
    assert git(alpha_remote, "branch", "--contains", new_pin).stdout.strip() == "", \
        "precondition: no branch of origin reaches the pin"

    done = gr2(fresh, "materialize", check=False)

    said = done.stdout + done.stderr
    assert done.returncode == 3, said
    assert "cannot be served by origin" in said and new_pin in said, said

"""`store init` records the branch each member is on, not the literal "main".

THE DEFECT, measured on grip dev ebd0371c: `_member_from_path` wrote `ref = "main"` and
`upstream = "origin/main"` for every member, from all three sources of members. A member on
`core/main` therefore had its pin checked against `origin/main`, and the first `store commit`
of a correct workspace was refused with exit 3, "pin ... is not on origin/main; push it first".
The shape that meets it is a repo whose "members" are namespaced branches of one url (a
tutorial workspace built that way), where no member is on `main` at all.

THE RULE, with each edge decided here rather than discovered later:
  * a member on an attached branch B records ref = B and upstream = its tracking ref when it
    has one, else `origin/B`;
  * a branch with no upstream falls back to `origin/B`, not a refusal and not `origin/main`:
    init is not where this is refused, and the coverage check at commit then names the branch
    that is actually missing;
  * a detached HEAD keeps today's defaults (main, origin/main), because a materialized
    workspace is detached at its pins and refusing there would break init;
  * a branch tracking a LOCAL branch (remote ".") has a tracking ref with no remote part, so it
    is treated as untracked and gets `origin/B`;
  * a local branch tracking a differently named upstream keeps both facts as the member states
    them (ref = the local branch, upstream = the tracking ref).

THE CONTROL IS ROW 2. A change that writes the branch name for every member would pass the
branch rows; the plain main/origin-main member must still get exactly today's two values.

Fixtures are real git with bare local origins and no network. Every branch of the shared
remote carries its OWN commit: a first version of this fixture put all branches on one commit,
so a pin was trivially on `origin/main` and the defect could not appear.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, run


def _shared_remote(tmp_path: Path, branches: tuple[str, ...]) -> Path:
    """A bare repo with `main` and each named branch, every branch with its own commit."""
    seed = tmp_path / "seed"
    run(tmp_path, "git", "init", "-b", "main", str(seed))
    configure_identity(seed)
    (seed / "README.md").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    for branch in branches:
        git(seed, "checkout", "-b", branch, "main")
        (seed / f"{branch.replace('/', '-')}.txt").write_text(f"{branch}\n")
        git(seed, "add", ".")
        git(seed, "commit", "-m", f"{branch} own work")
    git(seed, "checkout", "main")
    remote = tmp_path / "shared.git"
    run(tmp_path, "git", "clone", "--bare", str(seed), str(remote))
    return remote


def _clone(root: Path, remote: Path, name: str, branch: str = "main") -> Path:
    run(root, "git", "clone", "--branch", branch, str(remote), name)
    configure_identity(root / name)
    return root / name


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _members(root: Path) -> dict[str, dict]:
    document = tomllib.loads((root / "grip.toml").read_text())
    return {member["name"]: member for member in document["members"]}


def _gitlink(root: Path, member: str) -> str:
    return git(root, "ls-tree", "HEAD", "--", member).stdout.split()[2]


def test_a_one_url_workspace_of_namespaced_branches_commits_clean(tmp_path: Path) -> None:
    remote = _shared_remote(tmp_path, ("core/main", "web/main"))
    root = _root(tmp_path)
    core = _clone(root, remote, "demo-core", "core/main")
    web = _clone(root, remote, "demo-web", "web/main")

    assert gr2(root, "init").returncode == 0
    members = _members(root)
    assert (members["demo-core"]["ref"], members["demo-core"]["upstream"]) == ("core/main", "origin/core/main")
    assert (members["demo-web"]["ref"], members["demo-web"]["upstream"]) == ("web/main", "origin/web/main")

    first = gr2(root, "commit", "-m", "first", check=False)
    assert first.returncode == 0, f"a correct workspace must commit\n{first.stdout}\n{first.stderr}"
    core_head = git(core, "rev-parse", "HEAD").stdout.strip()
    web_head = git(web, "rev-parse", "HEAD").stdout.strip()
    assert core_head != web_head, "fixture: the two branches must differ or this proves nothing"
    assert _gitlink(root, "demo-core") == core_head
    assert _gitlink(root, "demo-web") == web_head
    assert gr2(root, "check", check=False).returncode == 0


def test_control_a_plain_main_member_still_gets_todays_values(tmp_path: Path) -> None:
    """THE CONTROL: without it, "write the branch name" for every member would pass row 1."""
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    _clone(root, remote, "plain", "main")

    assert gr2(root, "init").returncode == 0
    member = _members(root)["plain"]
    assert member["ref"] == "main"
    assert member["upstream"] == "origin/main"
    assert gr2(root, "commit", "-m", "first", check=False).returncode == 0


def test_a_branch_with_no_upstream_gets_origin_branch_and_commit_names_it(tmp_path: Path) -> None:
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    member = _clone(root, remote, "feat", "main")
    git(member, "checkout", "-b", "feature/x")
    (member / "x.txt").write_text("x\n")
    git(member, "add", ".")
    git(member, "commit", "-m", "x")
    assert git(member, "rev-parse", "--abbrev-ref", "feature/x@{upstream}", check=False).returncode != 0, "fixture: no upstream"

    assert gr2(root, "init").returncode == 0
    recorded = _members(root)["feat"]
    assert (recorded["ref"], recorded["upstream"]) == ("feature/x", "origin/feature/x")

    refused = gr2(root, "commit", "-m", "first", check=False)
    assert refused.returncode == 3, f"{refused.stdout}\n{refused.stderr}"
    assert "origin/feature/x" in refused.stderr and "push it first" in refused.stderr, refused.stderr
    assert "origin/main" not in refused.stderr, "the refusal must name the branch that is missing"

    git(member, "push", "origin", "feature/x")
    assert gr2(root, "commit", "-m", "first", check=False).returncode == 0


def test_a_detached_member_keeps_todays_defaults_and_init_does_not_refuse(tmp_path: Path) -> None:
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    member = _clone(root, remote, "pinned", "main")
    git(member, "checkout", "--detach")

    init = gr2(root, "init", check=False)
    assert init.returncode == 0, f"{init.stdout}\n{init.stderr}"
    recorded = _members(root)["pinned"]
    assert (recorded["ref"], recorded["upstream"]) == ("main", "origin/main")
    assert gr2(root, "commit", "-m", "first", check=False).returncode == 0


def test_a_local_branch_tracking_a_differently_named_upstream_keeps_both(tmp_path: Path) -> None:
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    member = _clone(root, remote, "feat", "main")
    git(member, "checkout", "-b", "feature", "--track", "origin/main")
    (member / "f.txt").write_text("f\n")
    git(member, "add", ".")
    git(member, "commit", "-m", "f")
    git(member, "push", "origin", "feature:main")

    assert gr2(root, "init").returncode == 0
    recorded = _members(root)["feat"]
    assert (recorded["ref"], recorded["upstream"]) == ("feature", "origin/main")
    assert gr2(root, "commit", "-m", "first", check=False).returncode == 0


def test_a_branch_tracking_a_local_branch_falls_back_to_origin_branch(tmp_path: Path) -> None:
    """Tracking a LOCAL branch makes `@{upstream}` read just "main", with no remote part, which
    `_member_coverage` would refuse as an invalid upstream."""
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    member = _clone(root, remote, "local", "main")
    git(member, "checkout", "-b", "local-x", "--track", "main")
    assert git(member, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").stdout.strip() == "main", "fixture"

    assert gr2(root, "init").returncode == 0
    recorded = _members(root)["local"]
    assert (recorded["ref"], recorded["upstream"]) == ("local-x", "origin/local-x")


def test_a_branch_tracking_a_slashed_local_branch_is_not_read_as_a_remote(tmp_path: Path) -> None:
    """THE ROW THAT PINS THE remote == "." CLAUSE. Tracking a local branch named `core/main`
    makes `@{upstream}` read "core/main", which has a slash and so parses as remote `core`,
    branch `main`: a coverage check against a remote that does not exist. Row 6's bare "main"
    never reached that clause, because the missing slash already sent it to the fallback; a
    mutant that dropped the clause survived row 6."""
    remote = _shared_remote(tmp_path, ("core/main",))
    root = _root(tmp_path)
    member = _clone(root, remote, "local", "core/main")
    git(member, "checkout", "-b", "local-y", "--track", "core/main")
    assert git(member, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").stdout.strip() == "core/main", "fixture"
    assert git(member, "config", "--get", "branch.local-y.remote").stdout.strip() == ".", "fixture: a local remote"

    assert gr2(root, "init").returncode == 0
    recorded = _members(root)["local"]
    assert (recorded["ref"], recorded["upstream"]) == ("local-y", "origin/local-y")


def _fork_member(tmp_path: Path, root: Path) -> Path:
    """A fork-shaped member: `origin` is the fork, the branch tracks `upstream/main`.

    `grip.toml` records the member's ORIGIN url and nothing else, so a recorded upstream whose
    remote is not `origin` names a remote that a fresh clone of the workspace does not have.
    """
    fork = _shared_remote(tmp_path, ())
    canonical = tmp_path / "canonical.git"
    run(tmp_path, "git", "clone", "--bare", str(fork), str(canonical))
    member = _clone(root, fork, "forked", "main")
    git(member, "remote", "add", "upstream", str(canonical))
    git(member, "fetch", "upstream")
    git(member, "branch", "--set-upstream-to=upstream/main", "main")
    assert git(member, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").stdout.strip() == "upstream/main", "fixture"
    return member


def test_a_fork_member_tracking_a_non_origin_remote_records_origin(tmp_path: Path) -> None:
    """A tracking ref is taken only when its remote is `origin`, the one remote grip.toml can
    carry; any other remote falls back to `origin/<branch>`."""
    root = _root(tmp_path)
    _fork_member(tmp_path, root)

    assert gr2(root, "init").returncode == 0
    recorded = _members(root)["forked"]
    assert (recorded["ref"], recorded["upstream"]) == ("main", "origin/main")


def test_a_fork_member_checks_clean_from_a_fresh_clone(tmp_path: Path) -> None:
    """THE CONSEQUENCE, the shape the read-2 probe found: init records `upstream/main`, the
    original root has that remote so commit and check pass there, and a FRESH clone plus
    materialize has only `origin`, so `store check` exits 5 "cannot fetch upstream"."""
    root = _root(tmp_path)
    member = _fork_member(tmp_path, root)
    assert gr2(root, "init").returncode == 0
    assert gr2(root, "commit", "-m", "first", check=False).returncode == 0

    root_remote = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", str(root_remote))
    git(root, "remote", "add", "origin", str(root_remote))
    git(root, "push", "-u", "origin", "HEAD:main")
    git(root_remote, "symbolic-ref", "HEAD", "refs/heads/main")
    fresh = tmp_path / "fresh"
    run(tmp_path, "git", "clone", "--no-checkout", str(root_remote), str(fresh))
    gr2(fresh, "materialize")
    assert git(fresh / "forked", "remote").stdout.split() == ["origin"], "fixture: the fresh clone has only origin"

    checked = gr2(fresh, "check", check=False)
    assert checked.returncode == 0, f"{checked.stdout}\n{checked.stderr}"
    assert git(fresh / "forked", "rev-parse", "HEAD").stdout.strip() == git(member, "rev-parse", "HEAD").stdout.strip()


def test_a_branch_whose_remote_is_origin_but_has_no_tracking_branch_gets_origin_branch(tmp_path: Path) -> None:
    """`branch.<B>.remote` can be set to origin without a merge ref, so `@{upstream}` reads
    nothing; the member must still record `origin/<B>`, not an empty upstream."""
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    member = _clone(root, remote, "half", "main")
    git(member, "checkout", "-b", "half-set")
    git(member, "config", "branch.half-set.remote", "origin")
    assert git(member, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "half-set@{upstream}", check=False).returncode != 0, "fixture: no upstream"

    assert gr2(root, "init").returncode == 0
    recorded = _members(root)["half"]
    assert (recorded["ref"], recorded["upstream"]) == ("half-set", "origin/half-set")

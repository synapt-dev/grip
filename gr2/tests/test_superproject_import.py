"""`workspace init --from-superproject`: the DECLARED entry, and the root's
declaration as the source of the member set.

THREE behaviours are pinned here. All were found by RUNNING the verb on
fixtures git built, and all three are the same shape -- a member set derived from
something other than the root's own declaration:

1. **The superproject line vanished on a root whose members are not checked
   out.** After a plain ``git clone`` without ``--recurse-submodules`` the
   member path is an EMPTY DIRECTORY, so it satisfies no membership predicate --
   there is no member on disk to satisfy one -- and ``_superproject_report``
   returned None. The line disappeared from exactly the root the launch copy
   leads with. `test_unmaterialized_members_are_counted_and_named` is its
   witness.

2. **A plain directory of repos claimed to BE a superproject.** The first fix
   for (1) keyed membership on the scan's ``pin`` field, but
   ``declared_repo_state`` sets ``pin`` from the member's own HEAD whenever the
   root pins nothing -- so every ordinary repo carried a ``pin`` and two
   directories reported ``superproject = true (members: 2 pinned)``. A control
   caught it: `test_a_plain_directory_of_repos_is_not_a_superproject` fails on
   that version and passes on this one.

3. **A ``.gitmodules`` alone is not a declaration of members.** The fixture
   measures git rather than quoting it:
   `git submodule status` reports ZERO members on such a root, because a
   ``.gitmodules`` is a lookup table and the tree's 160000 entries are what
   reference it. So the flag's gate is the gitlink alone, and the refusal names
   that case rather than claiming "no .gitmodules", which is false about the one
   root that needs the explanation.
   `test_a_gitmodules_without_a_gitlink_declares_no_members` is its witness.

Two branches a READER's probes went through are pinned here as well, because
neither was covered and an uncovered branch is a guard whose removal kills no
test: a member that is an ORDINARY CLONE (materialized, no module dir, so
``unknown`` rather than ``not materialized``), and a member at depth
(``libs/child3``), which only the declaration can find.

Every fixture is a real repository built by git, not a stand-in: the design
note's own proof used a public superproject, and the shapes that matter here (an
empty placeholder directory, ``.git/modules/<name>`` gitdirs, a detached member,
a nested gitlink) only exist when git makes them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import repo_maintenance_prototype as repo_proto
from gr2.python_cli.app import _superproject_report, app

from tests.conftest import make_cli_runner


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)
    (path / "README.md").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _superproject(tmp_path: Path, *, detached: bool = False, names=("child1", "child2")) -> Path:
    """A real superproject whose members are real submodules.

    ``protocol.file.allow=always`` is required by git 2.38+ to add a submodule
    from a local path; without it the add is refused and the fixture would be
    testing the refusal instead of the shape.
    """
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=root, check=True)
    for name in names:
        src = tmp_path / f"{name}-src"
        _init_repo(src)
        added = subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), name],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert added.returncode == 0, added.stderr
    subprocess.run(["git", "commit", "-qm", "add submodules"], cwd=root, check=True)
    if detached:
        for name in names:
            got = _run("checkout", "--detach", cwd=root / name)
            assert got.returncode == 0, got.stderr
    return root


def _plain_directory_of_repos(tmp_path: Path) -> Path:
    """Two repos side by side: no gitlinks in any tree, no ``.gitmodules``."""
    root = tmp_path / "plain"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    for name in ("a", "b"):
        _init_repo(root / name)
    return root


def _clone_without_members(root: Path, tmp_path: Path) -> Path:
    """The ordinary entry: a plain clone of a superproject, members NOT checked out.

    git creates an EMPTY directory at each member path, which is what makes this
    the discriminating fixture -- the path exists, is a directory, and is not a
    repository of any kind.
    """
    cloned = tmp_path / "cloned"
    got = _run("clone", "-q", str(root), str(cloned), cwd=tmp_path)
    assert got.returncode == 0, got.stderr
    for name in ("child1", "child2"):
        member = cloned / name
        assert member.is_dir(), f"fixture check: git creates the placeholder for {name}"
        assert not (member / ".git").exists(), f"fixture check: {name} is not materialized"
    return cloned


# ---------------------------------------------------------------------------
# The declared entry, on both materialization states
# ---------------------------------------------------------------------------


def test_materialized_superproject_is_adopted(tmp_path: Path) -> None:
    root = _superproject(tmp_path)
    result = make_cli_runner().invoke(
        app, ["workspace", "init", str(root), "--from-superproject"]
    )
    assert result.exit_code == 0, result.stdout
    assert "superproject = true" in result.stdout
    assert "2 pinned" in result.stdout
    assert "not materialized" not in result.stdout


def test_unmaterialized_members_are_counted_and_named(tmp_path: Path) -> None:
    """A declared member that is not on disk is still a member: the root's tree
    pins it. Before the fix the whole line was absent here, so the launch copy's
    leading claim disappeared on the one path it is written for.
    """
    root = _superproject(tmp_path)
    cloned = _clone_without_members(root, tmp_path)

    result = make_cli_runner().invoke(
        app, ["workspace", "init", str(cloned), "--from-superproject"]
    )
    assert result.exit_code == 0, result.stdout
    assert "superproject = true" in result.stdout
    assert "2 pinned" in result.stdout
    assert "2 not materialized" in result.stdout

    report = _superproject_report(cloned)
    assert report is not None
    assert report["members"] == 2
    assert report["not_materialized"] == 2
    # The members are NOT materialized, so nothing may claim a detached head:
    # a state read from the enclosing repo would be a fact about the root.
    assert report["detached"] == 0


def test_a_detached_member_reads_as_detached(tmp_path: Path) -> None:
    """The design note's first shape rule: the gitlink pins a SHA and the member
    has no branch, so the state must be read from the member's own HEAD."""
    root = _superproject(tmp_path, detached=True)
    report = _superproject_report(root)
    assert report is not None
    assert report["members"] == 2
    assert report["detached"] == 2


# ---------------------------------------------------------------------------
# The control that caught the second defect, and the refusal
# ---------------------------------------------------------------------------


def test_a_plain_directory_of_repos_is_not_a_superproject(tmp_path: Path) -> None:
    """A directory of repos pins nothing, so it is not a superproject.

    Two repos side by side pin nothing and declare nothing, so they are not
    members and this root is not a superproject. The intermediate version of
    ``_superproject_report`` reported ``superproject = true (members: 2 pinned)``
    here, because it keyed membership on a ``pin`` field that every ordinary
    repo carries -- so this test is the one that goes red if that shape returns.

    Asserted on the plain verb, with no flag, because the false claim appeared
    on the path a user who never heard of superprojects takes.
    """
    plain = _plain_directory_of_repos(tmp_path)
    assert _superproject_report(plain) is None

    result = make_cli_runner().invoke(app, ["workspace", "init", str(plain)])
    assert result.exit_code == 0, result.stdout
    assert "repo_count = 2" in result.stdout
    assert "superproject" not in result.stdout


def test_the_flag_refuses_a_root_that_declares_nothing(tmp_path: Path) -> None:
    """The flag's value is that it is the DECLARED entry: asked for the
    superproject path, a user must not silently get the directory path, because
    the two produce different member sets."""
    plain = _plain_directory_of_repos(tmp_path)
    result = make_cli_runner().invoke(
        app, ["workspace", "init", str(plain), "--from-superproject"]
    )
    assert result.exit_code == 1
    message = (result.stderr or "") + (result.stdout or "")
    assert "--from-superproject wants a root whose tree pins members" in message
    assert "no .gitmodules" in message
    # ...and the refusal names the verb that DOES suit this root.
    assert "gr2 workspace init" in message


def test_the_refusal_is_the_flags_and_not_the_roots(tmp_path: Path) -> None:
    """The control for the test above: the same root passes without the flag, so
    the refusal is the flag's condition rather than a root that cannot be
    initialized at all."""
    plain = _plain_directory_of_repos(tmp_path)
    assert (
        make_cli_runner()
        .invoke(app, ["workspace", "init", str(plain)])
        .exit_code
        == 0
    )


def test_a_gitmodules_without_a_gitlink_declares_no_members(tmp_path: Path) -> None:
    """A ``.gitmodules`` with no gitlink in the tree declares no members: the
    gitlink IS the declaration, because that is git's own rule.

    A root can carry a ``.gitmodules`` while its tree pins nothing -- the file
    is a lookup table, and the tree's 160000 entries are what reference it. An
    earlier version of ``_require_superproject`` accepted either half, so on
    this root the flag exited 0 and then printed no superproject line, and on
    the unmaterialized variant of it the run failed with "no git repos found",
    a message about the wrong problem.

    GIT IS THE ORACLE, and the fixture asserts it before asserting ours: both
    `git submodule status` and `git submodule status --cached` report ZERO
    members here. If a future git changes that, this test's fixture check fails
    and says so, rather than the behaviour quietly becoming correct in the
    other direction.
    """
    root = _superproject(tmp_path)
    got = _run("rm", "--cached", "-q", "child1", "child2", cwd=root)
    assert got.returncode == 0, got.stderr
    subprocess.run(["git", "commit", "-qm", "drop the gitlinks, keep .gitmodules"], cwd=root, check=True)

    # the shape the test claims to build
    assert (root / ".gitmodules").is_file(), "fixture: .gitmodules is still there"
    assert _run("ls-tree", "-r", "HEAD", cwd=root).stdout.count("160000") == 0, (
        "fixture: the tree pins nothing"
    )
    # GIT'S OWN ANSWER
    assert _run("submodule", "status", cwd=root).stdout.strip() == "", (
        "fixture/oracle: git reports no members on this root"
    )
    assert _run("submodule", "status", "--cached", cwd=root).stdout.strip() == "", (
        "fixture/oracle: and none cached either"
    )

    assert _superproject_report(root) is None

    result = make_cli_runner().invoke(
        app, ["workspace", "init", str(root), "--from-superproject"]
    )
    assert result.exit_code == 1
    message = (result.stderr or "") + (result.stdout or "")
    assert "pins none" in message
    # The sentence must NAME this case: a user with a .gitmodules has reason to
    # believe the root declares members, so "no .gitmodules" would be false
    # here and would leave them without the reason git disagrees.
    assert "carries a .gitmodules" in message
    assert "160000" in message


def test_a_root_that_pins_members_is_accepted_whatever_else_it_has(tmp_path: Path) -> None:
    """The control for the test above: the same fixture WITH its gitlinks passes,
    so the refusal is the gitlink condition and not something else about the
    root. Built by removing the gitlinks from one clone and leaving them in
    another, so the two differ in exactly the property under test."""
    root = _superproject(tmp_path)
    assert (
        make_cli_runner()
        .invoke(app, ["workspace", "init", str(root), "--from-superproject"])
        .exit_code
        == 0
    )


def test_a_member_that_is_an_ordinary_clone_is_not_called_unmaterialized(
    tmp_path: Path,
) -> None:
    """The branch between the two states, and it was uncovered until a reader's
    probe went through it.

    A member path can hold an ORDINARY CLONE rather than a submodule: it is
    materialized, it has its own ``.git`` directory, and there is no module
    directory to read a state from. Calling it ``not materialized`` would be
    false about a directory plainly there, so it reads ``unknown`` -- a state
    that could not be determined, which is a different answer from absent.
    """
    root = _superproject(tmp_path)
    member = root / "child1"
    src = tmp_path / "child1-src"
    shutil.rmtree(member)
    got = _run("clone", "-q", str(src), str(member), cwd=tmp_path)
    assert got.returncode == 0, got.stderr

    # the shape the test claims to build
    assert (member / ".git").is_dir(), "fixture: an ordinary clone, not a pointer file"
    assert repo_proto.is_submodule_member(member) is False
    assert repo_proto.is_own_toplevel(member) is True

    report = _superproject_report(root)
    assert report is not None, "a member that is an ordinary clone is still a member"
    assert report["members"] == 2
    assert report["not_materialized"] == 0, "it IS materialized"
    assert report["unknown"] == 1


def test_a_member_at_depth_is_found_by_its_declaration(tmp_path: Path) -> None:
    """The declaration path is not depth-1. A member at ``libs/child3`` is only
    visible because the root's tree pins that path; a directory walk of the root
    would see ``libs`` and, worse, answer every question about it from the
    ENCLOSING repository."""
    root = _superproject(tmp_path)
    src = tmp_path / "child3-src"
    _init_repo(src)
    added = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(src),
            "libs/child3",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "commit", "-qm", "add a depth-2 member"], cwd=root, check=True)

    assert "libs/child3" in repo_proto.root_gitlink_pins(root), (
        "fixture: the declaration records the nested path"
    )
    report = _superproject_report(root)
    assert report is not None
    assert report["members"] == 3


@pytest.mark.parametrize("shape", ["plain", "unmaterialized"])
def test_the_flag_never_writes_a_spec_when_it_refuses(tmp_path: Path, shape: str) -> None:
    """A refusal that leaves a spec behind is not a refusal. Checked for the
    plain root, and for the one superproject-shaped root that no longer refuses
    -- as a control that the assertion can see a spec when one IS written."""
    if shape == "plain":
        root = _plain_directory_of_repos(tmp_path)
        expect_spec = False
    else:
        root = _superproject(tmp_path)
        expect_spec = True
    result = make_cli_runner().invoke(
        app, ["workspace", "init", str(root), "--from-superproject"]
    )
    spec = root / ".grip" / "workspace_spec.toml"
    assert spec.exists() is expect_spec, (result.exit_code, result.stdout)

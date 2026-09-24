"""A submodule member is not a linked worktree, and the difference is measured
against git rather than asserted from its documented layout.

``is_linked_worktree`` detected a non-directory ``.git`` by lstat, which is
correct for the worktree POINTER FILE ``git worktree add`` produces and also
matched a SUBMODULE member, whose ``.git`` is a file too (``gitdir:
../.git/modules/<name>``). Two different things, told apart by nothing.

The cost was not cosmetic. ``workspace status`` flagged every member of a
converted superproject with the linked-worktree advice, ``run `gr2 workspace
convert-clone <path>` on each`` -- and that verb exits 1 there: it resolves its
clone source to ``<root>/.git/modules``, the module STORE rather than the
member, and git answers ``does not appear to be a git repository``. So a user
following our own advice on the entry path G12 is about reached a dead end in
our own tool, naming a directory that is not a repo.

The predicate ``is_linked_worktree`` has three callers (``workspace status``,
``repo status`` via ``inspect_repo``, and ``convert-clone``'s own guard), so the
fix belongs at the predicate rather than at the sentence one caller prints.

WHAT THE DISCRIMINATOR IS, and why this file does not rest on a layout claim the
author made: a linked worktree's admin dir carries a ``gitdir`` back-pointer
file holding the path to the worktree's ``.git``, and a submodule's module dir
carries none (it holds ``config``/``objects``/``refs`` instead, being a real git
dir but not a worktree-admin dir). ``test_gits_own_answer_agrees_with_the_
predicate`` corroborates that by asking git, over the same three fixtures, so
the predicate is checked against an oracle outside this code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import repo_maintenance_prototype as repo_proto
from gr2.python_cli import migration
from gr2.python_cli.app import _superproject_report, app

from tests.conftest import make_cli_runner


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True
    )


def fmt_args(object_format: str) -> list[str]:
    """The ``git init`` argument selecting an object format, if not the default."""
    return [] if object_format == "sha1" else [f"--object-format={object_format}"]


def _init_repo(path: Path, *, object_format: str = "sha1") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", *fmt_args(object_format)], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)
    (path / "README.md").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _superproject_with_member(
    tmp_path: Path, *, detached: bool, object_format: str = "sha1"
) -> tuple[Path, Path]:
    """A real superproject whose member is a real submodule.

    ``protocol.file.allow=always`` is required by git 2.38+ to add a submodule
    from a local path; without it the add is refused and the fixture would be
    testing the refusal instead of the shape.

    ``object_format`` is parametrized because a SHA-256 repository's detached
    HEAD is 64 hex characters rather than 40, which is a different length for
    anything reading HEAD, and not a different shape for git.
    """
    src = tmp_path / "member-src"
    _init_repo(src, object_format=object_format)

    root = tmp_path / "super"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "master", *fmt_args(object_format)], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=root, check=True)
    added = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(src),
            "member1",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "commit", "-qm", "add submodule"], cwd=root, check=True)

    member = root / "member1"
    if detached:
        got = _run("checkout", "--detach", cwd=member)
        assert got.returncode == 0, got.stderr
    return root, member


def _linked_worktree(tmp_path: Path) -> Path:
    canonical = tmp_path / "canonical"
    _init_repo(canonical)
    linked = tmp_path / "linked"
    got = _run("worktree", "add", "-b", "wt-branch", str(linked), cwd=canonical)
    assert got.returncode == 0, got.stderr
    return linked


def _plain_clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    _init_repo(origin)
    cloned = tmp_path / "cloned"
    got = _run("clone", "-q", str(origin), str(cloned), cwd=tmp_path)
    assert got.returncode == 0, got.stderr
    return cloned


# ---------------------------------------------------------------------------
# The predicate, against real fixtures and its own opposite controls
# ---------------------------------------------------------------------------


def test_a_submodule_member_is_not_a_linked_worktree(tmp_path: Path) -> None:
    _root, member = _superproject_with_member(tmp_path, detached=False)
    assert (member / ".git").is_file(), "the fixture must be the pointer-file shape"
    assert repo_proto.is_linked_worktree(member) is False


def test_a_real_linked_worktree_is_still_flagged(tmp_path: Path) -> None:
    """Control for the test above: a predicate that returned False for
    everything would pass that one and fail this one."""
    assert repo_proto.is_linked_worktree(_linked_worktree(tmp_path)) is True


def test_a_submodule_member_is_flagged_as_a_submodule_member(tmp_path: Path) -> None:
    _root, member = _superproject_with_member(tmp_path, detached=False)
    assert repo_proto.is_submodule_member(member) is True


def test_a_linked_worktree_is_not_a_submodule_member(tmp_path: Path) -> None:
    """The opposite control: the two shapes must not both satisfy one
    predicate, or the advice they carry could be swapped again."""
    assert repo_proto.is_submodule_member(_linked_worktree(tmp_path)) is False


def test_a_plain_clone_is_neither(tmp_path: Path) -> None:
    clone = _plain_clone(tmp_path)
    assert repo_proto.is_linked_worktree(clone) is False
    assert repo_proto.is_submodule_member(clone) is False


@pytest.mark.parametrize("separate_at", ["elsewhere.git", "modules/foo.git"])
def test_a_borrowed_gitdir_pointer_is_not_a_submodule_member(
    tmp_path: Path, separate_at: str
) -> None:
    """The control that fails if the predicate keys on a PATH COMPONENT.

    A `.git` file aimed outside a modules store is a third shape (a
    ``--separate-git-dir`` clone) and must not be claimed: claiming it would
    print superproject facts about a repo that is not in a superproject. The
    second parametrization puts the separate git dir under a directory NAMED
    ``modules``, because a predicate matching that name instead of reading what
    the module config says would call this a member -- and git itself says
    there is no superproject here.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    cloned = tmp_path / "cloned"
    # git will not create the intermediate directory itself, and a fixture that
    # fails to build would be testing the refusal instead of the shape.
    (tmp_path / separate_at).parent.mkdir(parents=True, exist_ok=True)
    got = _run(
        "clone",
        "-q",
        f"--separate-git-dir={tmp_path / separate_at}",
        str(origin),
        str(cloned),
        cwd=tmp_path,
    )
    assert got.returncode == 0, got.stderr
    assert (cloned / ".git").is_file(), "fixture check: the pointer-file shape"

    # git's own answer, which is the one that matters: not in a superproject.
    assert _run("rev-parse", "--show-superproject-working-tree", cwd=cloned).stdout.strip() == ""

    assert repo_proto.is_linked_worktree(cloned) is False
    assert repo_proto.is_submodule_member(cloned) is False

    result = make_cli_runner().invoke(app, ["workspace", "init", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "superproject" not in result.stdout


def test_a_sha256_detached_member_is_still_counted(tmp_path: Path) -> None:
    """A detached HEAD in a SHA-256 repository is 64 hex characters. A reader
    that accepted exactly 40 saw no state at all, dropped the member out of the
    count, and took `superproject` off init's output ENTIRELY -- the report
    vanishing is the failure, not a wrong number in it."""
    root, member = _superproject_with_member(
        tmp_path, detached=True, object_format="sha256"
    )
    head = (root / ".git" / "modules" / "member1" / "HEAD").read_text().strip()
    assert len(head) == 64 and all(c in "0123456789abcdef" for c in head.lower()), (
        "fixture check: a SHA-256 detached HEAD is 64 hex characters"
    )

    assert repo_proto.is_submodule_member(member) is True
    assert repo_proto.submodule_member_state(member) == "detached"

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout
    assert "superproject = true" in result.stdout
    assert "1 pinned" in result.stdout
    assert "1 detached" in result.stdout


def test_a_member_with_an_unrecognised_head_is_still_counted(tmp_path: Path) -> None:
    """The same failure in the other direction: a HEAD that is neither a ref
    nor an object id leaves the member's STATE unknown, and it must still be
    counted rather than silently dropped out of the report.

    The count is asserted through ``_superproject_report``, the function that
    builds it, rather than through `workspace init`'s output -- a HEAD git
    itself cannot read stops the repo being scanned at all, so an end-to-end
    assertion here would be measuring the scanner rather than the count.
    """
    root, _member = _superproject_with_member(tmp_path, detached=True)
    (root / ".git" / "modules" / "member1" / "HEAD").write_text("not-a-ref-or-an-object-id\n")

    assert repo_proto.is_submodule_member(root / "member1") is True
    assert repo_proto.submodule_member_state(root / "member1") == "unknown"

    report = _superproject_report(root, [{"name": "member1", "path": "member1"}])
    assert report is not None, "an unreadable state must not remove the report"
    assert report["members"] == 1
    assert report["detached"] == 0
    assert report["unknown"] == 1


def test_a_git_dir_pointing_its_worktree_elsewhere_is_not_a_member(
    tmp_path: Path,
) -> None:
    """The property the predicate actually keys on, pinned as a property: the
    module config's ``core.worktree`` must point BACK AT THIS MEMBER.

    A git dir that sets a ``worktree`` key pointing somewhere else is not this
    repo's module dir, and claiming it would print superproject facts about a
    repo that is not in a superproject. Without this, keying on "the config has
    a worktree key" would pass every other test in this file.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    cloned = tmp_path / "cloned"
    git_dir = tmp_path / "modules" / "foo.git"
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    got = _run(
        "clone", "-q", f"--separate-git-dir={git_dir}", str(origin), str(cloned), cwd=tmp_path
    )
    assert got.returncode == 0, got.stderr

    config = git_dir / "config"
    text = config.read_text()
    assert "[core]\n" in text, "fixture check: the config has a core section"
    config.write_text(text.replace("[core]\n", "[core]\n\tworktree = ../../elsewhere\n", 1))

    assert repo_proto.is_submodule_member(cloned) is False
    result = make_cli_runner().invoke(app, ["workspace", "init", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "superproject" not in result.stdout


def test_gits_own_answer_agrees_with_the_predicate(tmp_path: Path) -> None:
    """The oracle: ``rev-parse --show-superproject-working-tree`` is non-empty
    exactly when the repo is a submodule, and empty for a linked worktree. The
    predicate is required to agree with it on both fixtures, so the
    discriminator is checked against git and not only against itself."""
    _root, member = _superproject_with_member(tmp_path, detached=False)
    worktree = _linked_worktree(tmp_path)

    member_answer = _run("rev-parse", "--show-superproject-working-tree", cwd=member)
    worktree_answer = _run("rev-parse", "--show-superproject-working-tree", cwd=worktree)

    assert member_answer.stdout.strip(), "fixture check: the member IS in a superproject"
    assert worktree_answer.stdout.strip() == "", "fixture check: a worktree is not"

    assert repo_proto.is_submodule_member(member) is bool(member_answer.stdout.strip())
    assert repo_proto.is_submodule_member(worktree) is bool(worktree_answer.stdout.strip())

    # A SECOND oracle for the worktree side, and one that does not look at the
    # admin dir's contents at all: git's own structural answer. `--git-dir`
    # differs from `--git-common-dir` for a worktree (its gitdir is a per-tree
    # admin dir over a shared common dir) and is identical for a submodule.
    # Without this the predicate's marker would be corroborated only by the
    # fixture the marker itself was derived from.
    for fixture, expected_worktree in ((member, False), (worktree, True)):
        git_dir = _run("rev-parse", "--path-format=absolute", "--git-dir", cwd=fixture)
        common_dir = _run(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=fixture
        )
        assert git_dir.returncode == 0, git_dir.stderr
        assert common_dir.returncode == 0, common_dir.stderr
        assert (git_dir.stdout.strip() != common_dir.stdout.strip()) is expected_worktree
        assert repo_proto.is_linked_worktree(fixture) is expected_worktree


# ---------------------------------------------------------------------------
# The three callers, each of which was printing or acting on the wrong answer
# ---------------------------------------------------------------------------


def test_inspect_repo_does_not_call_a_submodule_member_a_linked_worktree(
    tmp_path: Path,
) -> None:
    """`repo status` derives from the same predicate, so it was wrong the same
    way -- the instance I found was in `workspace status`."""
    _root, member = _superproject_with_member(tmp_path, detached=False)
    status = repo_proto.inspect_repo(member)
    assert status.is_git_repo
    assert status.linked_worktree is False


def test_workspace_status_does_not_advise_convert_clone_for_a_submodule_member(
    tmp_path: Path,
) -> None:
    root, member = _superproject_with_member(tmp_path, detached=False)
    grip = root / ".grip"
    grip.mkdir(parents=True, exist_ok=True)
    (grip / "workspace_spec.toml").write_text(
        'workspace_name = "test"\n\n'
        '[[repos]]\n'
        'name = "member1"\n'
        'path = "member1"\n'
        f'url = "file://{tmp_path / "member-src"}"\n'
    )

    status = migration.workspace_status(root)
    assert status["linked_worktrees"] == []
    assert status["submodule_members"] == [str(member)]

    rendered = migration.render_status(status)
    assert str(member) in rendered
    assert "submodule" in rendered.lower()
    assert "convert-clone" not in rendered


def test_convert_clone_refuses_a_submodule_member_naming_the_submodule(
    tmp_path: Path,
) -> None:
    """The verb our own advice used to name, travelled for real. It must refuse
    with a reason about the submodule, not the generic not-a-worktree line,
    because the generic line is what sent the user here."""
    _root, member = _superproject_with_member(tmp_path, detached=False)
    result = make_cli_runner().invoke(app, ["workspace", "convert-clone", str(member)])
    assert result.exit_code == 1
    message = (result.stderr or "") + (result.stdout or "")
    assert "submodule" in message.lower()
    assert (member / ".git").read_text().strip() == "gitdir: ../.git/modules/member1"


# ---------------------------------------------------------------------------
# The entry report: the thing the launch copy leads with, currently unsaid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("detached,expected_detached", [(True, 1), (False, 0)])
def test_workspace_init_names_the_superproject(
    tmp_path: Path, detached: bool, expected_detached: int
) -> None:
    """`repo_count = 1` is the whole report today. The declaration being
    adopted matters more than the count: this is a superproject, the root is
    adopted, and its branches and worktree are untouched. The detached count is
    parametrized so the number is shown to move with the fixture rather than
    being a constant that happens to match."""
    root, _member = _superproject_with_member(tmp_path, detached=detached)
    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "superproject = true" in out
    assert "1 pinned" in out
    assert f"{expected_detached} detached" in out
    assert "untouched" in out


def test_workspace_init_does_not_claim_a_superproject_when_there_is_none(
    tmp_path: Path,
) -> None:
    """Control: the line is a claim about this root, not boilerplate printed
    for every workspace. Without this, `superproject = true` on a plain
    workspace would pass the test above."""
    plain = tmp_path / "plain"
    _init_repo(plain)
    (plain / "other").mkdir()
    _init_repo(plain / "other")
    result = make_cli_runner().invoke(app, ["workspace", "init", str(plain)])
    assert result.exit_code == 0, result.stdout
    assert "superproject" not in result.stdout

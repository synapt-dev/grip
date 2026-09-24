"""`spec validate` must report a declared repo path that holds a plain directory.

Measured 2026-09-24: a workspace whose declared repo path was a
plain, non-repository directory passed validation with ZERO issues. The cause is
the helper, not the check: ``validate_spec`` asked ``is_git_repo``, which answers
git's ``--is-inside-work-tree`` and is therefore TRUE for any directory inside a
checkout -- and a workspace root is a checkout. So the question was answered for
the enclosing repository and the conflicting path read as a repo.

WHY THIS FIXTURE GIT-INITS THE ROOT, asserted rather than assumed: in a plain
temp directory the read-through helper answers False, the conflict IS reported,
and the defect does not reproduce at all. The root being a repository is the
precondition for the test to have anything to say.

The PAIR is here for the same reason the #1116 tests carry one: the
same fixture with a REAL checkout at that path must report no
``repo_path_conflict``, or the check is noise a reader learns to skip.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gr2.python_cli import gitops, spec_apply
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner
from tests.test_materialize_pin import _superproject_with_a_divergent_pair


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)


def _workspace_with_a_declared_repo(tmp_path: Path, *, checkout: bool) -> Path:
    """A workspace that is ITSELF a repository, declaring one repo at ``repos/m``.

    ``checkout=True`` makes ``repos/m`` a real repository; ``False`` leaves a
    plain directory there -- the shape that used to validate clean.
    """
    root = tmp_path / "ws"
    _init_repo(root)
    member = root / "repos" / "m"
    if checkout:
        _init_repo(member)
    else:
        member.mkdir(parents=True)
        (member / "a-file.txt").write_text("not a repository\n")

    grip = root / ".grip"
    grip.mkdir(parents=True, exist_ok=True)
    (grip / "workspace_spec.toml").write_text(
        'workspace_name = "ws"\n\n'
        '[[repos]]\nname = "m"\npath = "repos/m"\nurl = "https://example.com/m.git"\n'
    )
    return root


def test_the_fixture_root_is_a_repository(tmp_path: Path) -> None:
    """FIXTURE GUARD. If the workspace root were not a repository, the read-through
    helper would answer about nothing, the conflict would be reported by the OLD
    code, and this file would pass while proving nothing. So the precondition is
    asserted, and the two helpers are shown to disagree on exactly this shape."""
    root = _workspace_with_a_declared_repo(tmp_path, checkout=False)
    plain = root / "repos" / "m"

    assert gitops.is_repo_root(root) is True, "the workspace root is a repository"
    assert gitops.is_git_repo(plain) is True, "the read-through helper speaks for the ENCLOSING repo"
    assert gitops.is_repo_root(plain) is False, "and the plain path is not itself one"


def test_a_plain_directory_at_a_declared_repo_path_is_reported(tmp_path: Path) -> None:
    """THE WITNESS. This is what passed validation clean before the fix."""
    root = _workspace_with_a_declared_repo(tmp_path, checkout=False)

    issues = spec_apply.validate_spec(root)

    conflicts = [i for i in issues if i.code == "repo_path_conflict"]
    assert conflicts, f"a plain directory at a declared repo path must be reported, got {issues}"
    assert any(str(root / "repos" / "m") in i.message for i in conflicts), (
        "the report names the path, so a reader can act on it"
    )


def test_a_real_checkout_at_the_same_path_is_not_reported(tmp_path: Path) -> None:
    """THE PAIR. Without this, the check could report everything and still pass."""
    root = _workspace_with_a_declared_repo(tmp_path, checkout=True)

    issues = spec_apply.validate_spec(root)

    conflicts = [i for i in issues if i.code == "repo_path_conflict"]
    assert conflicts == [], f"a real checkout must not be reported as a conflict, got {conflicts}"


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout


def test_an_empty_placeholder_is_not_a_conflict_and_the_first_run_works(tmp_path: Path) -> None:
    """THE REGRESSION WITNESS. A plain `git clone` of a superproject creates the
    submodule mount points and leaves them EMPTY until `submodule update --init`,
    so an empty directory at a declared repo path is the ordinary state of a
    freshly cloned workspace, not a conflict.

    Without the exemption this shape is called a conflict: `spec validate` goes
    to rc 1 and `materialize` aborts with nothing materialized, on the exact path
    the from-superproject entry exists to serve. The assertions
    run the whole first run, not just the validator, because that is where the
    damage landed.
    """
    root, pins = _superproject_with_a_divergent_pair(tmp_path)
    plain = tmp_path / "plain"
    subprocess.run(["git", "clone", "-q", str(root), str(plain)], check=True)
    for member in ("diverging", "converging"):
        placeholder = plain / member
        assert placeholder.is_dir() and not any(placeholder.iterdir()), (
            f"precondition: {member} is an EMPTY placeholder after a plain clone"
        )

    rc, out = _cli("workspace", "init", str(plain), "--from-superproject")
    assert rc == 0, out
    assert [i for i in spec_apply.validate_spec(plain) if i.code == "repo_path_conflict"] == [], (
        "an empty placeholder is not a repo_path_conflict"
    )
    rc, out = _cli("spec", "validate", str(plain))
    assert rc == 0, f"spec validate must not refuse a freshly cloned workspace: {out}"

    rc, out = _cli("workspace", "materialize", str(plain), "--yes")
    assert rc == 0, f"materialize must run on a freshly cloned workspace: {out}"
    unit = plain / "agents" / "default" / "home"
    for member, want in pins.items():
        got = _run("rev-parse", "HEAD", cwd=unit / member).stdout.strip()
        assert got == want, f"{member} landed on {got[:12]}, the root pins {want[:12]}"


def test_an_unreadable_member_directory_is_reported_not_a_crash(tmp_path: Path) -> None:
    """An unreadable directory at a declared repo path must ANSWER, not raise.

    The exemption asks whether the directory is EMPTY, and `iterdir()` on a
    chmod-000 directory raises PermissionError. Unguarded, that turned the
    answer the old code gave here (`is_git_repo` catches PermissionError and
    says False, so a conflict is reported) into a traceback, and the CLI exited
    1 with NO output at all -- which is worse than a wrong answer, because a
    reader cannot tell the command never ran.

    An unreadable directory is not an empty one: we cannot know, so it is not
    exempt and the conflict is reported.
    """
    root = _workspace_with_a_declared_repo(tmp_path, checkout=False)
    member = root / "repos" / "m"
    (member / "a-file.txt").unlink()
    assert not any(member.iterdir()), "precondition: the member holds nothing"
    os.chmod(member, 0)
    try:
        issues = spec_apply.validate_spec(root)  # must not raise
        conflicts = [i for i in issues if i.code == "repo_path_conflict"]
        assert conflicts, f"an unreadable member directory must be reported, got {issues}"
    finally:
        os.chmod(member, 0o755)

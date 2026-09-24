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

The PAIR is here for the same reason it was in the materialize-pin range: the
same fixture with a REAL checkout at that path must report no
``repo_path_conflict``, or the check is noise a reader learns to skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import gitops, spec_apply


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

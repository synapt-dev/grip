"""The generated `.gitmodules` names each member's branch, so plain `git submodule update --remote` follows it.

THE DEFECT, measured on grip dev 73d95ebb: `grip.toml` records a member's branch (`ref = "dev"`, `upstream =
"origin/dev"`), but the `.gitmodules` gr2 generates from it carries only `path` and `url`. Git reads the branch to
follow from `submodule.<name>.branch` and, with none, follows the remote's HEAD. So after `git submodule init`,
`git submodule update --remote` moved a member recorded on `dev` to MAIN's tip, a commit not on dev, and nothing
said so. That is the plain-git spelling of "advance each member to its branch", which is the reason to write it.

THE RULE, with each edge decided here:
  * the line is the UPSTREAM's branch, not the local `ref`: `submodule.<name>.branch` names a branch on the
    member's remote, and a local `mine` tracking `origin/main` must follow `main` (row 2);
  * a branch name containing `/` is kept whole (`origin/feat/greet` -> `feat/greet`, row 3);
  * a member detached at init records the default upstream, `origin/main`, and declares `main` (row 4);
  * an upstream with a line break is refused like the other fields, since `.gitmodules` cannot carry it (row 5);
  * `workspace init --from-superproject` reads the `.gitmodules` branch as the member's declared branch. A member
    attached to a local branch whose upstream IS that declared branch keeps its local name (`mine` tracking
    `origin/main` stays `mine`, row 6): the line names the remote branch, so the two agree and only the name differs.
    When the attached branch tracks something else, the declaration still outranks it, as ruled (row 7).

Fixtures are real git with bare local origins and no network. Git refuses a local-path submodule URL by default,
so the `update --remote` row names `protocol.file.allow`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.prototypes import repo_maintenance_prototype as repo_proto
from gr2.python_cli.app import app
from gr2.python_cli.grip_cli import NativeStoreRefusal, render_gitmodules
from tests.conftest import make_cli_runner
from tests.test_store_git_native_smoke import configure_identity, git, gr2, run


def _store(tmp_path: Path, setup) -> Path:
    """A store with one member `beta`, cloned from a bare origin that has `main` and `dev`, shaped by `setup`."""
    seed = tmp_path / "seed"
    run(tmp_path, "git", "init", "-q", "-b", "main", str(seed))
    configure_identity(seed)
    (seed / "f").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "branch", "dev")
    git(seed, "branch", "feat/greet")
    run(tmp_path, "git", "clone", "-q", "--bare", str(seed), str(tmp_path / "beta.git"))
    root = tmp_path / "workspace"
    root.mkdir()
    run(root, "git", "clone", "-q", str(tmp_path / "beta.git"), "beta")
    configure_identity(root / "beta")
    setup(root / "beta")
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    gr2(root, "commit", "-m", "first")
    return root


def _declared_branch(root: Path) -> str:
    got = git(root, "config", "-f", ".gitmodules", "--get", "submodule.beta.branch", check=False)
    return got.stdout.strip() if got.returncode == 0 else "<none>"


def _push_one(tmp_path: Path, branch: str) -> str:
    """One new commit on `branch` of beta's origin, made from a separate clone; returns it."""
    other = tmp_path / f"other-{branch.replace('/', '-')}"
    run(tmp_path, "git", "clone", "-q", "-b", branch, str(tmp_path / "beta.git"), str(other))
    configure_identity(other)
    (other / "f").write_text(f"on {branch}\n")
    git(other, "commit", "-q", "-am", f"on {branch}")
    git(other, "push", "-q", "origin", branch)
    return git(other, "rev-parse", "HEAD").stdout.strip()


def _on_dev(member: Path) -> None:
    git(member, "checkout", "-q", "-b", "dev", "--track", "origin/dev")


def test_a_member_on_dev_declares_dev_and_a_plain_update_remote_follows_it(tmp_path: Path) -> None:
    root = _store(tmp_path, _on_dev)
    assert _declared_branch(root) == "dev"
    main_tip = _push_one(tmp_path, "main")
    dev_tip = _push_one(tmp_path, "dev")
    assert main_tip != dev_tip  # the row discriminates only if the two branches differ

    git(root, "submodule", "init", "beta")
    moved = run(root, "git", "-c", "protocol.file.allow=always", "submodule", "update", "--remote", "beta", check=False)

    assert moved.returncode == 0, f"{moved.stdout}\n{moved.stderr}"
    assert git(root / "beta", "rev-parse", "HEAD").stdout.strip() == dev_tip


def test_the_branch_is_the_upstreams_not_the_local_ref(tmp_path: Path) -> None:
    root = _store(tmp_path, lambda m: git(m, "checkout", "-q", "-b", "mine", "--track", "origin/main"))
    spec = (root / "grip.toml").read_text()
    assert 'ref = "mine"' in spec and 'upstream = "origin/main"' in spec, spec  # the fixture's precondition
    assert _declared_branch(root) == "main"


def test_a_branch_name_with_a_slash_is_kept_whole(tmp_path: Path) -> None:
    root = _store(tmp_path, lambda m: git(m, "checkout", "-q", "-b", "feat/greet", "--track", "origin/feat/greet"))
    assert _declared_branch(root) == "feat/greet"


def test_a_member_detached_at_init_declares_the_default_upstream_branch(tmp_path: Path) -> None:
    root = _store(tmp_path, lambda m: git(m, "checkout", "-q", "--detach", "HEAD"))
    assert 'upstream = "origin/main"' in (root / "grip.toml").read_text()
    assert _declared_branch(root) == "main"


def test_an_upstream_with_a_line_break_is_refused() -> None:
    member = {"name": "beta", "path": "beta", "remote": "https://example.invalid/beta.git", "upstream": "origin/x\ny"}
    with pytest.raises(NativeStoreRefusal, match="upstream contains a line break"):
        render_gitmodules([member])


def _workspace_ref(root: Path) -> str:
    done = make_cli_runner().invoke(app, ["workspace", "init", str(root), "--from-superproject"])
    assert done.exit_code == 0, done.output
    return repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0].ref


def test_workspace_init_keeps_a_local_branch_that_tracks_the_declared_branch(tmp_path: Path) -> None:
    root = _store(tmp_path, lambda m: git(m, "checkout", "-q", "-b", "mine", "--track", "origin/main"))
    assert _declared_branch(root) == "main"  # the precondition: the line names the remote branch
    assert _workspace_ref(root) == "mine"


def test_workspace_init_keeps_the_declared_branch_when_the_local_branch_tracks_another(tmp_path: Path) -> None:
    """The twin: `mine` re-pointed at origin/dev after the store recorded origin/main. The declaration (main) and the
    attached branch now disagree, and the ruled precedence holds: the declared branch is recorded."""
    root = _store(tmp_path, lambda m: git(m, "checkout", "-q", "-b", "mine", "--track", "origin/main"))
    git(root / "beta", "branch", "-q", "--set-upstream-to", "origin/dev", "mine")
    assert _declared_branch(root) == "main"
    assert _workspace_ref(root) == "main"

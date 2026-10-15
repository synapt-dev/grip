"""A coverage refusal names both ways out: push the pin, or switch the member to the branch it belongs on.

THE DEFECT, measured on grip dev 7694b7e6: `gr2 store migrate` from a member on an unpushed local feature branch
exits 3 with "pin ... is not on origin/<branch>; push it first", and `gr2 store commit` (and the store's pre-commit
hook) refuse an unpushed pin with the same single exit. Pushing is one way out; the other, often the right one, is
that the member is on the wrong branch.

THE RULE: each refusal keeps "push it first" and adds "or switch <member> to the branch it should be recorded on",
ending in the verb to run again (migrate, or commit).

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

from pathlib import Path

from gr2.python_cli import grip as grip_mod

from tests.test_store_git_native_smoke import configure_identity, git, gr2, run
from tests.test_store_init_branch_inference import _clone, _root, _shared_remote
from tests.test_store_materialize_installs_hooks import _cloned


def _said(result) -> str:
    return result.stdout + result.stderr


def test_migrate_from_an_unpushed_feature_branch_names_both_exits(tmp_path: Path) -> None:
    remote = _shared_remote(tmp_path, ())
    root = _root(tmp_path)
    member = _clone(root, remote, "demo-core", "main")
    configure_identity(member)
    git(member, "checkout", "-q", "-b", "feat/local")
    git(member, "commit", "-q", "--allow-empty", "-m", "local only")
    grip_mod.grip_init(root)
    grip_mod.grip_snapshot(root, {"demo-core": member}, message="alpha snapshot")

    refused = gr2(root, "migrate", "--json", check=False)

    assert refused.returncode == 3, _said(refused)
    assert "push it first" in refused.stderr, refused.stderr
    assert "or switch demo-core to the branch it should be recorded on, then migrate" in refused.stderr, refused.stderr
    assert not (root / ".git").exists()


def test_commit_of_an_unpushed_pin_on_the_recorded_branch_names_both_exits(tmp_path: Path) -> None:
    root = _cloned(tmp_path)
    gr2(root, "materialize")
    beta = root / "beta"
    configure_identity(beta)
    git(beta, "commit", "-q", "--allow-empty", "-m", "unpushed, not moved")

    verb = gr2(root, "commit", "-m", "pin unpushed", check=False)
    git(root, "add", "beta")
    hook = run(root, "git", "commit", "-q", "-m", "pin unpushed", check=False)

    want = "push it first, or switch beta to the branch it should be recorded on, then commit again"
    assert verb.returncode == 3 and want in _said(verb), _said(verb)
    assert hook.returncode != 0 and want in _said(hook), _said(hook)


def test_commit_of_an_unpushed_pin_on_a_moved_branch_names_both_exits(tmp_path: Path) -> None:
    root = _cloned(tmp_path)
    gr2(root, "materialize")
    beta = root / "beta"
    configure_identity(beta)
    git(beta, "checkout", "-q", "-b", "feat/y")
    git(beta, "commit", "-q", "--allow-empty", "-m", "unpushed, moved")

    verb = gr2(root, "commit", "-m", "pin unpushed", check=False)
    git(root, "add", "beta")
    hook = run(root, "git", "commit", "-q", "-m", "pin unpushed", check=False)

    want = "push it first, or switch beta to the branch it should be recorded on, then commit again"
    assert verb.returncode == 3 and want in _said(verb), _said(verb)
    assert hook.returncode != 0 and want in _said(hook), _said(hook)

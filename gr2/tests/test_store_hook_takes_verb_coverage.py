"""The store's pre-commit hook gives the same answer as `gr2 store commit`, from the same state.

THE DEFECT, measured on grip dev 55842d42: a member on a new branch whose commit is pushed to that branch on its
origin, but not to the declared upstream. `gr2 store commit` records it (rc 0; it adopts the branch whose upstream
covers the pin), while a plain `git add <member> && git commit` through the hook refused it with "... is not on
origin/dev; push it first", advice that does nothing for a pin that is already pushed.

THE RULE: the hook and the verb call one per-member resolution, so they record the same pin, branch and upstream,
and refuse the same states. A pin on no remote branch is still refused by both.

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, run
from tests.test_store_materialize_installs_hooks import _cloned


def _second_clone(tmp_path: Path) -> Path:
    """Another plain clone of the same pushed root, materialized, so the two record paths start from one state."""
    second = tmp_path / "second"
    run(tmp_path, "git", "clone", "-q", str(tmp_path / "root.git"), str(second))
    configure_identity(second)
    gr2(second, "materialize")
    return second


def _feature_commit(member: Path, branch: str, push: bool) -> str:
    configure_identity(member)
    git(member, "checkout", "-q", "-b", branch)
    git(member, "commit", "-q", "--allow-empty", "-m", branch)
    if push:
        git(member, "push", "-q", "origin", branch)
    return git(member, "rev-parse", "HEAD").stdout.strip()


def _follow(member: Path, branch: str) -> None:
    """The same member state in another clone: on `branch`, tracking origin's `branch`."""
    configure_identity(member)
    git(member, "fetch", "-q", "origin")
    git(member, "checkout", "-q", "-b", branch, "--track", f"origin/{branch}")


def _record(root: Path) -> tuple[str, str]:
    """What a root commit recorded for beta: its gitlink and the committed grip.toml."""
    return git(root, "ls-tree", "HEAD", "beta").stdout.split()[2], git(root, "show", "HEAD:grip.toml").stdout


def test_a_pin_pushed_to_a_feature_branch_commits_through_the_hook_as_through_the_verb(tmp_path: Path) -> None:
    by_verb = _cloned(tmp_path)
    gr2(by_verb, "materialize")
    head = _feature_commit(by_verb / "beta", "feat/x", push=True)
    by_hook = _second_clone(tmp_path)
    _follow(by_hook / "beta", "feat/x")
    assert git(by_hook / "beta", "rev-parse", "HEAD").stdout.strip() == head   # one state, two clones

    verb = gr2(by_verb, "commit", "-m", "pin feat/x", check=False)
    git(by_hook, "add", "beta")
    hook = run(by_hook, "git", "commit", "-q", "-m", "pin feat/x", check=False)

    assert verb.returncode == 0, verb.stderr
    assert hook.returncode == 0, hook.stderr
    assert _record(by_hook)[0] == head
    assert _record(by_hook) == _record(by_verb)   # same pin, and the same grip.toml (ref and upstream included)


def test_control_a_pin_on_no_remote_branch_is_refused_by_both(tmp_path: Path) -> None:
    by_verb = _cloned(tmp_path)
    gr2(by_verb, "materialize")
    before = _record(by_verb)
    _feature_commit(by_verb / "beta", "feat/y", push=False)

    verb = gr2(by_verb, "commit", "-m", "pin unpushed", check=False)
    git(by_verb, "add", "beta")
    hook = run(by_verb, "git", "commit", "-q", "-m", "pin unpushed", check=False)

    assert verb.returncode != 0 and hook.returncode != 0, (verb.stderr, hook.stderr)
    assert "push it first" in hook.stderr, hook.stderr
    assert _record(by_verb) == before

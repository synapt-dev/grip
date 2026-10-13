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


def _record(root: Path) -> tuple[str, str, str]:
    """What a root commit recorded for beta: its gitlink, the committed grip.toml and the committed .gitmodules."""
    return (git(root, "ls-tree", "HEAD", "beta").stdout.split()[2], git(root, "show", "HEAD:grip.toml").stdout,
            git(root, "show", "HEAD:.gitmodules", check=False).stdout)


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
    assert _record(by_hook) == _record(by_verb)   # same pin, grip.toml (ref and upstream) and .gitmodules


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


def test_a_pushed_pin_no_branch_adopts_is_refused_naming_where_it_is_by_both(tmp_path: Path) -> None:
    """Detached at a pin that is on origin/feat/x: no branch to adopt, so both paths refuse; but the pin IS pushed,
    so the refusal names the remote branch that holds it, never "push it first"."""
    by_verb = _cloned(tmp_path)
    gr2(by_verb, "materialize")
    head = _feature_commit(by_verb / "beta", "feat/x", push=True)
    git(by_verb / "beta", "checkout", "-q", "--detach", head)

    verb = gr2(by_verb, "commit", "-m", "pin detached", check=False)
    git(by_verb, "add", "beta")
    hook = run(by_verb, "git", "commit", "-q", "-m", "pin detached", check=False)

    for said in (verb, hook):
        assert said.returncode != 0, said.stderr
        assert "push it first" not in said.stderr, said.stderr
        assert "origin/feat/x" in said.stderr, said.stderr


def test_the_hook_judges_the_staged_pin_when_the_checkout_has_moved_on(tmp_path: Path) -> None:
    """Staged: the feat/x head that is on origin/feat/x. Then the member commits once more, locally. The hook judges
    what the root commit will carry (the staged pin) and adopts feat/x, whose upstream covers it."""
    root = _cloned(tmp_path)
    gr2(root, "materialize")
    staged = _feature_commit(root / "beta", "feat/x", push=True)
    git(root, "add", "beta")
    git(root / "beta", "commit", "-q", "--allow-empty", "-m", "local only")

    hook = run(root, "git", "commit", "-q", "-m", "pin staged", check=False)

    assert hook.returncode == 0, hook.stderr
    gitlink, toml, _ = _record(root)
    assert gitlink == staged
    assert "feat/x" in toml, toml


def test_a_staged_pushed_pin_with_the_checkout_on_a_branch_with_no_upstream_is_refused_naming_where_it_is(
    tmp_path: Path,
) -> None:
    root = _cloned(tmp_path)
    gr2(root, "materialize")
    _feature_commit(root / "beta", "feat/x", push=True)
    git(root, "add", "beta")
    git(root / "beta", "checkout", "-q", "-b", "scratch")

    hook = run(root, "git", "commit", "-q", "-m", "pin staged", check=False)

    assert hook.returncode != 0, hook.stderr
    assert "push it first" not in hook.stderr and "origin/feat/x" in hook.stderr, hook.stderr


def test_the_verb_declares_an_adopted_branch_in_gitmodules_as_in_grip_toml(tmp_path: Path) -> None:
    """Measured on dev 883c7810: after adopting feat/x, `store commit` wrote ref = "feat/x" to grip.toml and left
    branch = "dev" in .gitmodules, rendered from the members before adoption; one record, two answers."""
    root = _cloned(tmp_path)
    gr2(root, "materialize")
    _feature_commit(root / "beta", "feat/x", push=True)

    assert gr2(root, "commit", "-m", "pin feat/x", check=False).returncode == 0

    declared = git(root, "config", "-f", ".gitmodules", "--get", "submodule.beta.branch").stdout.strip()
    toml = git(root, "show", "HEAD:grip.toml").stdout
    assert 'ref = "feat/x"' in toml, toml
    assert declared == "feat/x", declared

"""Coverage answers WHETHER THE WORK LANDED, not whether a commit object is present.

`_coverage_of(path, pin, upstream)` says `reachable` (the pin is an ancestor), `landed_by_patch` (the same change is on
upstream under other commits: a rebase-merge, or a squash-merge) or `uncovered`. `store check` refuses a landed pin by
telling the author to re-pin, never to push it, and `materialize` names a covering ref that vanished instead of saying
only that the fetch failed.
"""
from __future__ import annotations

from pathlib import Path

from gr2.python_cli import grip_cli as gc
from tests.test_store_full_mode_carrier import _advance, _full_store
from tests.test_store_git_native_smoke import configure_identity, git, gr2


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    configure_identity(repo)
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "up")
    return repo


def _commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _land_on_up(repo: Path, make) -> None:
    """Run ``make`` on a branch cut from `up`'s tip, then move `up` to the result and come back to main."""
    git(repo, "checkout", "-q", "-b", "scratch", "up")
    make()
    git(repo, "branch", "-f", "up", "scratch")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-D", "scratch")


def test_an_ancestor_pin_is_reachable(tmp_path):
    repo = _repo(tmp_path)
    pin = _commit(repo, "a.txt", "a\n", "work")
    git(repo, "branch", "-f", "up", "main")

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_REACHABLE


def test_a_rebase_merged_pin_is_landed_by_patch(tmp_path):
    repo = _repo(tmp_path)
    pin = _commit(repo, "a.txt", "a\n", "work")
    _land_on_up(repo, lambda: (git(repo, "cherry-pick", "-x", pin), git(repo, "commit", "-q", "--amend", "-m", "reworded")))

    assert git(repo, "merge-base", "--is-ancestor", pin, "up", check=False).returncode != 0, "the pin must not be reachable"
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_LANDED


def test_a_rebase_merged_two_commit_pin_is_landed_by_patch(tmp_path):
    """Each commit lands separately, so the combined diff of the pin is NOT one commit on upstream: only the
    commit-by-commit read can answer this one."""
    repo = _repo(tmp_path)
    first = _commit(repo, "a.txt", "a\n", "first")
    pin = _commit(repo, "b.txt", "b\n", "second")
    # Each cherry-pick is reworded so neither is the pin's own commit object again (an identical object would be an
    # ancestor, and only the last commit would be left for the combined read to answer).
    _land_on_up(repo, lambda: (git(repo, "cherry-pick", first), git(repo, "commit", "-q", "--amend", "-m", "first again"),
                               git(repo, "cherry-pick", pin), git(repo, "commit", "-q", "--amend", "-m", "second again")))

    assert git(repo, "merge-base", pin, "up").stdout.strip() == git(repo, "rev-parse", "main~2").stdout.strip()
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_LANDED


def test_an_empty_commit_pin_is_uncovered_even_when_upstream_moved(tmp_path):
    """No patch means nothing to match: an empty set of change-ids must not read as 'all of them landed'."""
    repo = _repo(tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")
    pin = git(repo, "rev-parse", "HEAD").stdout.strip()
    _land_on_up(repo, lambda: _commit(repo, "u.txt", "u\n", "upstream moved"))

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def test_a_squash_merged_two_commit_pin_is_landed_by_patch(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "a.txt", "a\n", "first")
    pin = _commit(repo, "b.txt", "b\n", "second")
    _land_on_up(repo, lambda: (git(repo, "merge", "--squash", pin), git(repo, "commit", "-q", "-m", "squashed")))

    assert git(repo, "merge-base", "--is-ancestor", pin, "up", check=False).returncode != 0
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_LANDED


def test_a_pin_with_the_same_message_but_a_different_change_is_uncovered(tmp_path):
    """The control: a commit upstream that only LOOKS like the pin (same message, different bytes) is not the work."""
    repo = _repo(tmp_path)
    pin = _commit(repo, "a.txt", "a\n", "work")
    _land_on_up(repo, lambda: _commit(repo, "a.txt", "something else\n", "work"))

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def test_a_pin_carrying_one_landed_and_one_unlanded_commit_is_uncovered(tmp_path):
    repo = _repo(tmp_path)
    first = _commit(repo, "a.txt", "a\n", "first")
    pin = _commit(repo, "b.txt", "b\n", "second")
    _land_on_up(repo, lambda: git(repo, "cherry-pick", first))

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def test_a_pin_with_no_common_history_is_uncovered(tmp_path):
    repo = _repo(tmp_path)
    git(repo, "checkout", "-q", "--orphan", "other")
    git(repo, "rm", "-rfq", ".")
    pin = _commit(repo, "z.txt", "z\n", "unrelated")

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def _recorded_then_rewritten(tmp_path: Path, *, squash: bool) -> tuple[Path, str]:
    """A non-full store whose alpha pin was covered by `origin/side`, after which `side` was rewritten under it: the
    same change on a different commit, as a squash- or rebase-merge leaves it."""
    root = _full_store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text(
        text.replace('mode = "full"\n', "").replace('upstream = "origin/main"', 'upstream = "origin/side"', 1), encoding="utf-8")
    alpha = root / "alpha"
    pin = _advance(root, "alpha", push=False)
    git(alpha, "push", "origin", "HEAD:refs/heads/side")
    done = gr2(root, "commit", "-m", "alpha covered by side")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    base = git(alpha, "rev-parse", "HEAD~1").stdout.strip()
    git(alpha, "checkout", "-q", "-b", "rewrite", base)
    if squash:
        git(alpha, "merge", "--squash", pin)
        git(alpha, "commit", "-q", "-m", "squashed")
    else:
        git(alpha, "cherry-pick", pin)
        git(alpha, "commit", "-q", "--amend", "-m", "reworded")
    git(alpha, "push", "-f", "origin", "HEAD:refs/heads/side")
    git(alpha, "checkout", "-q", "--detach", pin)
    git(alpha, "branch", "-D", "rewrite")
    return root, pin


def test_store_check_refuses_a_landed_pin_by_saying_re_pin_not_push(tmp_path):
    root, pin = _recorded_then_rewritten(tmp_path, squash=True)

    refused = gr2(root, "check", check=False)

    out = f"{refused.stdout}\n{refused.stderr}"
    assert refused.returncode == 3, out
    assert "landed on origin/side" in out and "re-pin" in out, out
    assert "push it first" not in out, out


def test_store_check_still_says_push_it_first_for_a_pin_that_never_landed(tmp_path):
    """The control: the same store with the pin's work absent from upstream keeps the old advice."""
    root, pin = _recorded_then_rewritten(tmp_path, squash=True)
    alpha = root / "alpha"
    git(alpha, "push", "-f", "origin", f"{pin}~1:refs/heads/side")

    refused = gr2(root, "check", check=False)

    out = f"{refused.stdout}\n{refused.stderr}"
    assert refused.returncode == 3, out
    assert "push it first" in out, out


def _vanished_covering_ref(tmp_path: Path, *, with_layer: bool, delete_ref: bool = True,
                           single_branch: bool = True) -> tuple[Path, str]:
    """A full-mode record whose covering ref `side` is deleted from origin after the record was made, read from a fresh
    single-branch clone that never held the pin."""
    root = _full_store(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    (root / "grip.toml").write_text(text.replace('upstream = "origin/main"', 'upstream = "origin/side"', 1), encoding="utf-8")
    alpha = root / "alpha"
    pin = _advance(root, "alpha", push=False)
    git(alpha, "push", "origin", "HEAD:refs/heads/side")
    done = gr2(root, "commit", "-m", "alpha covered by side")
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    url = git(alpha, "remote", "get-url", "origin").stdout.strip()
    if with_layer:
        # The layered twin of the record: the pin is on no remote branch, so the record carries a layer instead.
        git(alpha, "push", "origin", "--delete", "side")
        text = (root / "grip.toml").read_text(encoding="utf-8")
        (root / "grip.toml").write_text(text.replace("origin/side", "origin/main", 1), encoding="utf-8")
        done = gr2(root, "commit", "-m", "alpha layered")
        assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    elif delete_ref:
        git(alpha, "push", "origin", "--delete", "side")
    git(tmp_path, "clone", str(root), "fresh")
    fresh = tmp_path / "fresh"
    configure_identity(fresh)
    git(fresh, "clone", "--no-local", *(("--single-branch", "--branch", "main") if single_branch else ()), url, "alpha")
    assert (git(fresh / "alpha", "cat-file", "-t", pin, check=False).returncode == 0) is (not single_branch)
    return fresh, pin


def test_materialize_names_a_vanished_covering_ref_when_nothing_else_holds_the_pin(tmp_path):
    fresh, pin = _vanished_covering_ref(tmp_path, with_layer=False)

    made = gr2(fresh, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode == 3, out
    assert "full_mode_covering_ref_vanished" in out and "side" in out and pin in out, out


def test_materialize_with_a_layer_does_not_need_the_vanished_covering_ref(tmp_path):
    """The control: the same deleted ref, but the record carries a layer, so the pin still arrives."""
    fresh, pin = _vanished_covering_ref(tmp_path, with_layer=True)

    made = gr2(fresh, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode == 0, out
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin


def test_an_unreachable_origin_is_not_told_the_covering_ref_vanished(tmp_path):
    """The ref is NOT deleted; origin cannot be reached. Saying the ref is gone would send the author to re-pin."""
    fresh, pin = _vanished_covering_ref(tmp_path, with_layer=False, delete_ref=False)
    git(fresh / "alpha", "remote", "set-url", "origin", str(tmp_path / "nonexistent.git"))

    made = gr2(fresh, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode == 3, out
    assert "vanished" not in out and "gone from origin" not in out, out
    assert "cannot be fetched from origin" in out, out


def test_a_failed_covering_fetch_proceeds_when_the_clone_already_holds_the_pin(tmp_path):
    fresh, pin = _vanished_covering_ref(tmp_path, with_layer=False, delete_ref=False, single_branch=False)
    git(fresh / "alpha", "remote", "set-url", "origin", str(tmp_path / "nonexistent.git"))

    made = gr2(fresh, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert made.returncode == 0, out
    assert git(fresh / "alpha", "rev-parse", "HEAD").stdout.strip() == pin


def test_a_landed_then_reverted_pin_is_uncovered(tmp_path):
    """The change landed once and upstream's tip no longer has it: re-pinning to the tip would drop the work."""
    repo = _repo(tmp_path)
    pin = _commit(repo, "a.txt", "a\n", "work")

    def make():
        git(repo, "cherry-pick", pin)
        git(repo, "commit", "-q", "--amend", "-m", "reworded")
        git(repo, "revert", "--no-edit", "HEAD")

    _land_on_up(repo, make)

    assert git(repo, "cat-file", "-e", "up:a.txt", check=False).returncode != 0
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def test_a_pin_landed_reverted_and_landed_again_is_landed(tmp_path):
    """The control: land, revert, land again is net landed (two lands against one revert)."""
    repo = _repo(tmp_path)
    pin = _commit(repo, "a.txt", "a\n", "work")

    def make():
        git(repo, "cherry-pick", pin)
        git(repo, "commit", "-q", "--amend", "-m", "first landing")
        git(repo, "revert", "--no-edit", "HEAD")
        git(repo, "cherry-pick", pin)
        git(repo, "commit", "-q", "--amend", "-m", "second landing")

    _land_on_up(repo, make)

    assert git(repo, "cat-file", "-e", "up:a.txt", check=False).returncode == 0
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_LANDED


def test_a_squash_landed_then_reverted_pin_is_uncovered(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "a.txt", "a\n", "first")
    pin = _commit(repo, "b.txt", "b\n", "second")

    def make():
        git(repo, "merge", "--squash", pin)
        git(repo, "commit", "-q", "-m", "squashed")
        git(repo, "revert", "--no-edit", "HEAD")

    _land_on_up(repo, make)

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def _merge_pin(repo: Path, *, own_content: bool) -> tuple[str, str, str]:
    """A on main, B on a side branch, then B merged into main with --no-ff. With ``own_content`` the merge itself adds
    a file neither parent has. Returns (pin, a, b), the pin being the merge."""
    a = _commit(repo, "a.txt", "a\n", "A")
    git(repo, "checkout", "-q", "-b", "side", "main~1")
    b = _commit(repo, "b.txt", "b\n", "B")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    if own_content:
        (repo / "merge-only.txt").write_text("only in the merge\n")
        git(repo, "add", "merge-only.txt")
    git(repo, "commit", "-q", "-m", "merge side")
    return git(repo, "rev-parse", "HEAD").stdout.strip(), a, b


def _land_commits(repo: Path, *commits: str) -> None:
    def make():
        for i, commit in enumerate(commits):
            git(repo, "cherry-pick", commit)
            git(repo, "commit", "-q", "--amend", "-m", f"landed {i}")
    _land_on_up(repo, make)


def test_a_merge_with_content_of_its_own_is_uncovered_when_only_its_parents_landed(tmp_path):
    repo = _repo(tmp_path)
    pin, a, b = _merge_pin(repo, own_content=True)
    _land_commits(repo, a, b)

    assert git(repo, "cat-file", "-e", "up:merge-only.txt", check=False).returncode != 0, "the merge's content never landed"
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def test_a_merge_with_content_of_its_own_is_uncovered_when_its_parents_landed_squashed(tmp_path):
    repo = _repo(tmp_path)
    pin, a, b = _merge_pin(repo, own_content=True)
    _land_on_up(repo, lambda: (git(repo, "cherry-pick", "--no-commit", a), git(repo, "cherry-pick", "--no-commit", b),
                               git(repo, "commit", "-q", "-m", "A and B squashed")))

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def test_a_merge_pin_squashed_whole_onto_upstream_is_landed_by_patch(tmp_path):
    repo = _repo(tmp_path)
    pin, _a, _b = _merge_pin(repo, own_content=True)
    _land_on_up(repo, lambda: (git(repo, "merge", "--squash", pin), git(repo, "commit", "-q", "-m", "squashed")))

    assert git(repo, "merge-base", "--is-ancestor", pin, "up", check=False).returncode != 0
    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_LANDED


def test_a_plain_merge_whose_parents_landed_one_by_one_is_uncovered(tmp_path):
    """The named cost of answering a merge from the combined diff alone: a merge that adds nothing, whose parents landed
    as separate commits, reads uncovered (the old advice, push it first), because no single upstream commit carries the
    combined change. Safe direction; the re-pin it misses is not lost work."""
    repo = _repo(tmp_path)
    pin, a, b = _merge_pin(repo, own_content=False)
    _land_commits(repo, a, b)

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED


def _indent_only_pin(repo: Path) -> str:
    """A Python change that only moves `return y` under an `if`: indentation is the whole change, and its meaning."""
    _commit(repo, "f.py", "def f(x, y):\n    if x:\n        pass\n    return y\n", "f")
    git(repo, "branch", "-f", "up", "main")
    return _commit(repo, "f.py", "def f(x, y):\n    if x:\n        pass\n        return y\n", "indent return")


def test_an_indentation_only_pin_landed_is_landed_by_patch(tmp_path):
    repo = _repo(tmp_path)
    pin = _indent_only_pin(repo)
    _land_on_up(repo, lambda: (git(repo, "cherry-pick", pin), git(repo, "commit", "-q", "--amend", "-m", "landed")))

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_LANDED


def test_an_indentation_only_pin_landed_then_reverted_is_uncovered(tmp_path):
    """A whitespace-stripping patch-id hashes this change and its revert the same, so the revert never un-landed it."""
    repo = _repo(tmp_path)
    pin = _indent_only_pin(repo)

    def make():
        git(repo, "cherry-pick", pin)
        git(repo, "commit", "-q", "--amend", "-m", "landed")
        git(repo, "revert", "--no-edit", "HEAD")

    _land_on_up(repo, make)

    assert gc._coverage_of(repo, pin, "up") == gc.COVERAGE_UNCOVERED

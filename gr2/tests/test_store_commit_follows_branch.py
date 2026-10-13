"""`store commit` records the branch a member MOVED to, not the one it was on at init.

THE DEFECT, measured in an agent run of the store phase (gr2 from grip dev f95a297d): after a
member moved from `feat/greet` to merged `main` and `store commit` recorded the merged main as its
pin, `grip.toml` still read `ref = "feat/greet"` and `upstream = "origin/feat/greet"`. The record's
pin and its declared branch disagreed, and every operator in the run moved members off the feature
branch with raw git. Measured again here on
grip dev d2a9d29 before the fix: in compat mode the same move is REFUSED, exit 3, "alpha pin ... is
not on origin/feat/greet; push it first", for a merge commit that is on origin/main.

THE RULE, with each edge decided here:
  * a member attached to a branch OTHER than its recorded ref records that branch and its upstream
    by the reader `store init` uses (`_infer_member_branch`) WHEN THAT UPSTREAM COVERS THE PIN, so a
    merge commit on main is checked against origin/main and recorded there;
  * a moved branch whose upstream does not cover the pin is not adopted: `git checkout -b wip` with
    no new commit keeps the record, which still covers the pin (row 5); when neither upstream covers
    it, the refusal names the branch the member is on as well as the recorded upstream (row 6);
  * a member still on its recorded ref keeps exactly what it had, including an upstream the owner
    declared by hand (the control, row 2; break 08 in test_store_break_attempts.py is the declared
    upstream's own witness);
  * a DETACHED member (what `store materialize` leaves) keeps its recorded ref and upstream: the
    init-time default for a detached HEAD is main/origin/main; writing that would erase a record.

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, run


def _member_on_feature(tmp_path: Path) -> tuple[Path, Path]:
    """One member `alpha` on a pushed `feat/greet`, the store initialised and committed."""
    seed = tmp_path / "seed"
    run(tmp_path, "git", "init", "-b", "main", str(seed))
    configure_identity(seed)
    (seed / "README.md").write_text("base\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    remote = tmp_path / "alpha.git"
    run(tmp_path, "git", "clone", "--bare", str(seed), str(remote))
    root = tmp_path / "workspace"
    root.mkdir()
    run(root, "git", "clone", str(remote), "alpha")
    alpha = root / "alpha"
    configure_identity(alpha)
    git(alpha, "checkout", "-b", "feat/greet")
    (alpha / "greet.txt").write_text("hello\n")
    git(alpha, "add", ".")
    git(alpha, "commit", "-m", "greet")
    git(alpha, "push", "-u", "origin", "feat/greet")
    assert gr2(root, "init").returncode == 0
    gr2(root, "commit", "-m", "on the feature")
    return root, alpha


def _member(root: Path) -> dict:
    return tomllib.loads((root / "grip.toml").read_text())["members"][0]


def _merge_to_main(alpha: Path) -> str:
    """Merge feat/greet into main with a merge commit (on main, NOT on feat/greet) and push main."""
    git(alpha, "checkout", "main")
    git(alpha, "merge", "--no-ff", "-m", "merge feat/greet", "feat/greet")
    git(alpha, "push", "origin", "main")
    return git(alpha, "rev-parse", "HEAD").stdout.strip()


def test_after_the_merge_store_commit_records_main(tmp_path: Path) -> None:
    root, alpha = _member_on_feature(tmp_path)
    assert (_member(root)["ref"], _member(root)["upstream"]) == ("feat/greet", "origin/feat/greet")
    merged = _merge_to_main(alpha)

    result = gr2(root, "commit", "-m", "on merged main", check=False)
    assert result.returncode == 0, (
        f"a merged main pin is on origin/main and must commit\n{result.stdout}\n{result.stderr}")
    member = _member(root)
    assert member["pin"] == merged
    assert (member["ref"], member["upstream"]) == ("main", "origin/main"), member


def test_a_member_that_stays_on_its_branch_keeps_its_record(tmp_path: Path) -> None:
    root, alpha = _member_on_feature(tmp_path)
    (alpha / "more.txt").write_text("more\n")
    git(alpha, "add", ".")
    git(alpha, "commit", "-m", "more")
    git(alpha, "push", "origin", "feat/greet")
    gr2(root, "commit", "-m", "more on the feature")
    member = _member(root)
    assert (member["ref"], member["upstream"]) == ("feat/greet", "origin/feat/greet"), member
    assert member["pin"] == git(alpha, "rev-parse", "HEAD").stdout.strip()


def test_a_detached_member_keeps_its_recorded_branch(tmp_path: Path) -> None:
    root, alpha = _member_on_feature(tmp_path)
    (alpha / "more.txt").write_text("more\n")
    git(alpha, "add", ".")
    git(alpha, "commit", "-m", "more")
    git(alpha, "push", "origin", "feat/greet")
    # main fast-forwarded to the same commit, so origin/main ALSO covers the pin: the detached
    # default (main/origin/main) would pass the coverage question, and only the detached guard
    # keeps the record from being rewritten to main
    git(alpha, "push", "origin", "HEAD:main")
    git(alpha, "checkout", "--detach", "HEAD")   # the shape `store materialize` leaves
    gr2(root, "commit", "-m", "detached at a pushed feature commit")
    member = _member(root)
    assert (member["ref"], member["upstream"]) == ("feat/greet", "origin/feat/greet"), member


def test_full_mode_records_merged_main_without_a_layer(tmp_path: Path) -> None:
    """The proof's own shape (full mode): before the fix the merged main was checked against
    origin/feat/greet, found on no such branch, and carried as a LAYER it did not need, while
    grip.toml kept feat/greet."""
    root, alpha = _member_on_feature(tmp_path)
    text = (root / "grip.toml").read_text(encoding="utf-8")
    assert 'mode = "full"\n\n[[members]]' not in text
    full = text.replace("\n\n[[members]]", '\nmode = "full"\n\n[[members]]', 1)
    (root / "grip.toml").write_text(full, encoding="utf-8")
    gr2(root, "commit", "-m", "flip to full")
    assert tomllib.loads((root / "grip.toml").read_text()).get("mode") == "full"
    _merge_to_main(alpha)

    result = gr2(root, "commit", "-m", "on merged main", check=False)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "carried as a layer" not in result.stdout, result.stdout
    member = _member(root)
    assert (member["ref"], member["upstream"]) == ("main", "origin/main"), member


def test_a_new_branch_with_no_new_commit_keeps_the_record(tmp_path: Path) -> None:
    """`git checkout -b wip` with no commit: origin/wip does not exist, and the recorded upstream
    still covers the pin, so the moved branch is NOT adopted and nothing is refused."""
    root, alpha = _member_on_feature(tmp_path)
    before = _member(root)
    git(alpha, "checkout", "-b", "wip")
    result = gr2(root, "commit", "-m", "on wip, nothing new", check=False)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "push it first" not in result.stdout + result.stderr
    member = _member(root)
    assert (member["ref"], member["upstream"], member["pin"]) == (
        before["ref"], before["upstream"], before["pin"]), member


def test_when_neither_upstream_covers_the_pin_the_refusal_names_the_moved_branch(
    tmp_path: Path,
) -> None:
    root, alpha = _member_on_feature(tmp_path)
    git(alpha, "checkout", "-b", "wip")
    (alpha / "wip.txt").write_text("unpushed\n")
    git(alpha, "add", ".")
    git(alpha, "commit", "-m", "unpushed on wip")
    result = gr2(root, "commit", "-m", "unpushed", check=False)
    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    said = result.stdout + result.stderr
    assert "origin/wip (it is on wip)" in said and "origin/feat/greet" in said, said
    assert _member(root)["ref"] == "feat/greet"

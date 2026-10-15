"""After `review merge` pushes a member's merge, the author's clone knows it: `<remote>/<into>` names the merged tip.

THE DEFECT, measured on grip dev ab2dfd8b in the five-member review cell: `review merge` pushed each member's merge
and left the author's `origin/main` at the pre-merge base. `gr2 branch main --base origin/main` then reset main to that
old tip with exit 0, and `gr2 store commit` recorded the PRE-merge pins with exit 0, so the next author pinned on a
stale base. The only way through was a hand `git fetch` in every member.

THE RULE: after the run, for every member whose remote now holds the merge, the clone's remote-tracking ref for the
target is fetched by name from the member remote's configured remote. A fetch that fails there never fails the merge
(the remote already holds it), but it is never silent either: the member is listed in the receipt's
`tracking_stale` with the exact command to run, and the CLI prints the same on stderr.
"""
from __future__ import annotations

import json
import tomllib

from typer.testing import CliRunner

from gr2.python_cli import merge_gate
from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, check, git, merge, world  # noqa: F401

runner = CliRunner()


def test_a_merge_moves_the_authors_tracking_ref_to_the_merged_tip(world):
    check(world)
    assert git(world["member"], "rev-parse", "origin/main") == world["base"], "precondition: the clone knows the base"

    code, receipt = merge(world)

    assert code == merge_gate.EXIT_MERGED, receipt
    merged = git(world["remote"], "rev-parse", "main")
    assert git(world["member"], "rev-parse", "origin/main") == merged
    assert receipt["tracking_stale"] == [], receipt


def test_after_a_merge_store_commit_records_the_merged_pin_with_no_hand_fetch(world):
    """The push-2 sequence: merge, put the member on main at origin/main, record. No `git fetch` anywhere."""
    check(world)
    code, receipt = merge(world)
    assert code == merge_gate.EXIT_MERGED, receipt

    switched = runner.invoke(app, ["branch", "main", "--base", "origin/main", "--repo-path", str(world["member"])])
    assert switched.exit_code == 0, switched.output
    committed = runner.invoke(app, ["store", "commit", "-m", "merged"])
    assert committed.exit_code == 0, committed.output

    merged = git(world["remote"], "rev-parse", "main")
    recorded = tomllib.loads((world["author"] / "grip.toml").read_text())["members"][0]["pin"]
    assert recorded == merged, f"recorded {recorded[:12]}, remote main {merged[:12]}, base {world['base'][:12]}"


def test_a_tracking_fetch_that_fails_after_the_push_is_named_in_the_receipt_and_on_stderr(world):
    """A held lock on the tracking ref makes the fetch fail after the push succeeded: the merge stands, the miss shows."""
    check(world)
    lock = world["member"] / ".git" / "refs" / "remotes" / "origin" / "main.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    try:
        result = runner.invoke(app, ["review", "merge", "--approvals", "0", str(world["author"]), world["review"],
                                     "--into", "main", "--from", "feat"])
    finally:
        lock.unlink()

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout.splitlines()[0])
    merged = git(world["remote"], "rev-parse", "main")
    assert receipt["members"][0]["final"] == "merged", receipt
    stale = receipt["tracking_stale"]
    assert [s["member"] for s in stale] == [receipt["members"][0]["key"]], receipt
    assert "git -C" in stale[0]["command"] and "fetch" in stale[0]["command"] and "refs/remotes/origin/main" in stale[0]["command"]
    assert stale[0]["command"] in result.stderr, result.stderr
    assert git(world["member"], "rev-parse", "origin/main") == world["base"], "the lock held the old tip"
    assert merged != world["base"]


def test_control_a_refused_merge_touches_no_tracking_ref(world):
    """No check record: the merge refuses, nothing is pushed, and the clone's tracking ref is left as it was."""
    before = git(world["member"], "rev-parse", "origin/main")

    code, receipt = merge(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert git(world["member"], "rev-parse", "origin/main") == before
    assert receipt["tracking_stale"] == [], receipt


def _respell_origin(world, url):
    git(world["member"], "remote", "set-url", "origin", url)
    git(world["member"], "fetch", "origin")  # the respelled remote works: it is the same repository
    assert git(world["member"], "rev-parse", "origin/main") == world["base"], "precondition: the clone knows the base"


def test_a_file_scheme_spelling_of_the_same_remote_is_still_moved(world):
    _respell_origin(world, f"file://{world['remote']}")
    check(world)

    code, receipt = merge(world)

    assert code == merge_gate.EXIT_MERGED, receipt
    assert git(world["member"], "rev-parse", "origin/main") == git(world["remote"], "rev-parse", "main")
    assert receipt["tracking_stale"] == [], receipt


def test_a_trailing_slash_spelling_of_the_same_remote_is_still_moved(world):
    _respell_origin(world, f"{world['remote']}/")
    check(world)

    code, receipt = merge(world)

    assert code == merge_gate.EXIT_MERGED, receipt
    assert git(world["member"], "rev-parse", "origin/main") == git(world["remote"], "rev-parse", "main")
    assert receipt["tracking_stale"] == [], receipt


def test_a_clone_whose_remotes_name_another_url_is_listed_never_passed_over(world, tmp_path):
    """origin is a second bare copy with the same history: no spelling of it is the review's remote, so the clone's
    tracking ref cannot be moved by name, and the member is listed with the URL to fetch, on the receipt and stderr."""
    other = tmp_path / "other.git"
    git(tmp_path, "clone", "--bare", "-q", world["remote"], other)
    _respell_origin(world, str(other))
    check(world)

    result = runner.invoke(app, ["review", "merge", "--approvals", "0", str(world["author"]), world["review"],
                                 "--into", "main", "--from", "feat"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout.splitlines()[0])
    stale = receipt["tracking_stale"]
    assert [s["member"] for s in stale] == [receipt["members"][0]["key"]], receipt
    assert str(world["remote"]) in stale[0]["command"] and "refs/remotes/origin/main" in stale[0]["command"]
    assert stale[0]["reason"] == f"no remote of this clone names {world['remote']}", stale
    assert stale[0]["command"] in result.stderr and stale[0]["reason"] in result.stderr, result.stderr
    assert git(world["member"], "rev-parse", "origin/main") == world["base"]


def test_url_key_treats_spellings_of_one_repository_as_one(tmp_path):
    key = merge_gate._url_key
    assert key("https://github.com/org/x.git", tmp_path) == key("https://github.com/org/x", tmp_path)
    assert key("git@github.com:org/x.git", tmp_path) == key("https://github.com/org/x", tmp_path)
    assert key("ssh://git@GitHub.com:22/org/x.git", tmp_path) == key("https://github.com/org/x/", tmp_path)
    assert key("https://github.com/org/x", tmp_path) != key("https://github.com/org/y", tmp_path)
    assert key("https://github.com/org/x", tmp_path) != key("https://gitlab.com/org/x", tmp_path)
    (tmp_path / "r.git").mkdir()
    assert key("r.git", tmp_path) == key(str(tmp_path / "r.git"), tmp_path) == key(f"file://{tmp_path}/r.git/", tmp_path)
    assert key(str(tmp_path / "r.git"), tmp_path) != key(str(tmp_path / "r"), tmp_path), "a local path keeps its .git"


def test_a_clone_with_no_remotes_has_nothing_to_leave_behind(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main", tmp_path / "bare-clone")
    assert merge_gate._refresh_tracking(tmp_path / "bare-clone", "https://github.com/org/x", "main") is None


"""`pr create --repo-path <member> --base <branch>` opens a one-branch PR with no review pinned first.

Until now `pr create` refused with "no pinned review" and the way out was `review pin` with a hand-read base and head.
With --repo-path the command pins inside itself: the head is the member checkout's HEAD (it must already be the tip of
the member's branch on the remote), the base is the LIVE tip of refs/heads/<base> read by ls-remote at that moment,
and the pin is written by the same writer `review pin` uses, so behind-must-be-0 and the already-on-remote rule are
its own. The workspace's other pinned reviews are never consulted to pick one: a pin that holds exactly the row is
reused, otherwise a new pin is made, and the PR opens for that id, named explicitly.
Fake adapter, no host; the fake https remote is rewritten to a local bare as test_pr_review_subject.py does."""
from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from gr2.python_cli import app as app_mod
from gr2.python_cli.app import app

from tests.test_pr_events import FakeAdapter
from tests.test_pr_review_subject import git, gr2

runner = CliRunner()
URL = "https://github.com/o/alpha.git"
DATE = "2026-01-01T00:00:00Z"  # a pin is a commit: fixed dates make the same rows the same id


@pytest.fixture
def one_branch(tmp_path, monkeypatch):
    """A native workspace holding member `alpha`, on `feat/one` at one commit past main, pushed; nothing pinned."""
    isolated_config = tmp_path / "identity-gitconfig"
    isolated_config.write_text("[user]\n\tname = Fixture\n\temail = fixture@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for var in ("GR2_ACTOR", "SYNAPT_AGENT_ID", "SYNAPT_AGENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_DATE", DATE)
    monkeypatch.setenv("GIT_COMMITTER_DATE", DATE)
    author, bare, seed = tmp_path / "author", tmp_path / "alpha.git", tmp_path / "seed"
    author.mkdir()
    seed.mkdir()
    git(tmp_path, "init", "--bare", "-b", "main", bare)
    git(seed, "init", "-b", "main")
    (seed / "p.txt").write_text("alpha\n")
    git(seed, "add", "p.txt")
    git(seed, "commit", "-m", "base")
    git(seed, "push", bare, "main")
    git(author, "clone", "--branch", "main", bare, "alpha")
    git(author / "alpha", "remote", "set-url", "origin", URL)
    init = gr2(author, monkeypatch, "store", "init", author)
    assert init.exit_code == 0, init.output
    git(author, "config", "--local", f"url.file://{bare}.insteadOf", URL)
    member = author / "alpha"
    git(member, "switch", "-c", "feat/one")
    (member / "p.txt").write_text("alpha reviewed\n")
    git(member, "commit", "-am", "change")
    git(member, "push", bare, "feat/one")
    adapter = FakeAdapter()
    monkeypatch.setattr(app_mod.platform_ops, "get_platform_adapter", lambda name: adapter)
    monkeypatch.setattr(app_mod, "_effective_remote", lambda ws, remote: remote)  # the rewrite stands for github.com
    return dict(author=author, member=member, bare=bare, adapter=adapter, tmp=tmp_path,
                head=git(member, "rev-parse", "HEAD"), base=git(bare, "rev-parse", "main"))


def pr_create(w, monkeypatch, *extra):
    return gr2(w["author"], monkeypatch, "pr", "create", "--repo-path", w["member"], "--base", "main", "--json", *extra)


def pins(w) -> list[str]:
    return git(w["author"], "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/").splitlines()


def advance_remote_main(w, name: str) -> str:
    """Another clone moves the remote's main one commit on; the author's checkout is not told."""
    other = w["tmp"] / f"other-{name}"
    git(w["tmp"], "clone", "-q", "--branch", "main", w["bare"], other)
    (other / f"{name}.txt").write_text(name + "\n")
    git(other, "add", f"{name}.txt")
    git(other, "commit", "-m", name)
    git(other, "push", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def pinned_id(result) -> str:
    found = re.search(r"gr:[0-9a-f]{40}", result.output)
    assert found, result.output
    return found.group(0)


def shown(w, monkeypatch, rid: str) -> dict:
    out = gr2(w["author"], monkeypatch, "review", "show", rid, "--json")
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def test_a_one_branch_pr_pins_itself_and_opens_the_pr(one_branch, monkeypatch):
    w = one_branch
    made = pr_create(w, monkeypatch)
    assert made.exit_code == 0, made.output
    rid = pinned_id(made)  # the review id is printed
    [request] = w["adapter"].created
    assert (request.repo, request.head_branch, request.base_branch) == ("o/alpha", "feat/one", "main")
    assert len(pins(w)) == 1
    [member] = shown(w, monkeypatch, rid)["members"]
    assert (member["head"], member["base"]) == (w["head"], w["base"])
    # The pin is what a hand `review pin` of the same row writes: the same row at the same second is the same id.
    by_hand = gr2(w["author"], monkeypatch, "review", "pin", "--repo", "alpha", "--remote", URL, "--base", w["base"],
                  "--head", w["head"], "--ref", "refs/heads/main")
    assert by_hand.exit_code == 0, by_hand.output
    assert pinned_id(by_hand) == rid
    assert len(pins(w)) == 1
    # The head was pushed before it was pinned, so the pin records the order; no --ratified was needed.
    assert member["pushed_before_pin"] == "refs/heads/feat/one"
    assert "ratified_by" not in member


def test_b_a_head_that_is_not_on_the_remote_refuses_naming_gr2_push(one_branch, monkeypatch):
    w = one_branch
    (w["member"] / "p.txt").write_text("a later commit\n")
    git(w["member"], "commit", "-am", "later")  # HEAD is past the pushed tip
    behind = pr_create(w, monkeypatch)
    assert behind.exit_code == 2, behind.output
    assert "gr2 push" in behind.output and "not HEAD" in behind.output, behind.output
    git(w["member"], "switch", "-c", "never-pushed")  # a branch the remote does not have
    absent = pr_create(w, monkeypatch)
    assert absent.exit_code == 2 and "gr2 push" in absent.output and "absent" in absent.output, absent.output
    assert w["adapter"].created == [] and pins(w) == []


def test_c_a_base_that_moves_between_the_read_and_the_pin_refuses(one_branch, monkeypatch):
    w = one_branch
    read = advance_remote_main(w, "first")  # the live tip the helper will read; the checkout's own main is stale
    real = app_mod._live_remote_tips
    moved: list[str] = []

    def read_then_the_remote_moves(*args, **kwargs):
        got = real(*args, **kwargs)
        moved.append(advance_remote_main(w, "late"))
        return got

    monkeypatch.setattr(app_mod, "_live_remote_tips", read_then_the_remote_moves)
    made = pr_create(w, monkeypatch)
    assert made.exit_code == 2, made.output
    # The base handed to the pin is the one the helper read live, and the pin's own read caught the move.
    assert f"base_not_live_head: expected '{read}', observed '{moved[0]}'" in made.output, made.output
    assert w["adapter"].created == [] and pins(w) == []


def test_c2_the_base_is_the_live_remote_tip_not_the_checkouts_stale_one(one_branch, monkeypatch):
    w = one_branch
    live = advance_remote_main(w, "moved")
    assert git(w["member"], "rev-parse", "main") == w["base"] != live  # the checkout never saw the move
    made = pr_create(w, monkeypatch)
    assert made.exit_code == 0, made.output
    [member] = shown(w, monkeypatch, pinned_id(made))["members"]
    assert member["base"] == live and member["head"] == w["head"]
    assert len(w["adapter"].created) == 1


def pin_by_hand(w, monkeypatch, head: str, base: str | None = None) -> str:
    done = gr2(w["author"], monkeypatch, "review", "pin", "--repo", "alpha", "--remote", URL, "--base", base or w["base"],
               "--head", head, "--ref", "refs/heads/main")
    assert done.exit_code == 0, done.output
    return pinned_id(done)


def unrelated_pins(w, monkeypatch) -> list[str]:
    """Two pins of work that is not the checkout's HEAD: commits on branches the remote has never seen."""
    ids = []
    for name in ("other-a", "other-b"):
        git(w["member"], "switch", "-c", name, "feat/one")
        (w["member"] / f"{name}.txt").write_text(name + "\n")
        git(w["member"], "add", f"{name}.txt")
        git(w["member"], "commit", "-m", name)
        ids.append(pin_by_hand(w, monkeypatch, git(w["member"], "rev-parse", "HEAD")))
    git(w["member"], "switch", "feat/one")
    return ids


def groups(w) -> list[dict]:
    return [json.loads(p.read_text()) for p in (w["author"] / ".grip" / "pr_groups").glob("*.json")]


def test_d1_other_pins_are_not_consulted_a_new_pin_is_made_and_the_pr_opens_for_it(one_branch, monkeypatch):
    w = one_branch
    before = unrelated_pins(w, monkeypatch)
    assert len(before) == 2 and len(set(before)) == 2 and len(pins(w)) == 2
    made = pr_create(w, monkeypatch)
    assert made.exit_code == 0, made.output
    new = pinned_id(made)
    assert new not in before and len(pins(w)) == 3
    [group] = groups(w)
    assert group["review_target"] == new
    [request] = w["adapter"].created
    assert (request.repo, request.head_branch, request.base_branch) == ("o/alpha", "feat/one", "main")
    [member] = shown(w, monkeypatch, new)["members"]
    assert (member["head"], member["base"]) == (w["head"], w["base"])


def test_d2_a_pin_that_already_holds_exactly_the_row_is_reused_and_makes_no_ref(one_branch, monkeypatch):
    w = one_branch
    unrelated_pins(w, monkeypatch)
    exact = pin_by_hand(w, monkeypatch, w["head"])
    assert len(pins(w)) == 3
    made = pr_create(w, monkeypatch)
    assert made.exit_code == 0, made.output
    assert f"reusing {exact}" in made.output and "pinned gr:" not in made.output, made.output
    assert len(pins(w)) == 3  # no new ref
    [group] = groups(w)
    assert group["review_target"] == exact
    assert len(w["adapter"].created) == 1


def test_d2b_a_pin_of_the_same_head_over_a_stale_base_is_not_reused(one_branch, monkeypatch):
    w = one_branch
    stale = pin_by_hand(w, monkeypatch, w["head"])  # pinned over the base as it was
    live = advance_remote_main(w, "moved")
    made = pr_create(w, monkeypatch)
    assert made.exit_code == 0, made.output
    new = pinned_id(made)
    assert new != stale and len(pins(w)) == 2
    [group] = groups(w)
    assert group["review_target"] == new
    [member] = shown(w, monkeypatch, new)["members"]
    assert member["base"] == live


def test_without_repo_path_the_refusal_to_open_with_no_pin_is_unchanged(one_branch, monkeypatch):
    w = one_branch
    bare = gr2(w["author"], monkeypatch, "pr", "create", "--json")
    assert bare.exit_code != 0 and "no pinned review" in bare.output, bare.output
    assert w["adapter"].created == [] and pins(w) == []


@pytest.mark.parametrize("argv,words", [
    (["--base", "main", "--review", "gr:" + "0" * 40], "give one or the other"),
    ([], "needs --base"),
])
def test_repo_path_refuses_what_it_cannot_serve(one_branch, monkeypatch, argv, words):
    w = one_branch
    result = gr2(w["author"], monkeypatch, "pr", "create", "--repo-path", w["member"], *argv)
    assert result.exit_code == 2 and words in result.output, result.output
    assert w["adapter"].created == [] and pins(w) == []


def test_repo_path_beside_a_lane_unit_is_refused(one_branch, monkeypatch):
    w = one_branch
    result = gr2(w["author"], monkeypatch, "pr", "create", w["author"], "unit", "--repo-path", w["member"], "--base", "main")
    assert result.exit_code == 2 and "OWNER_UNIT" in result.output, result.output
    assert w["adapter"].created == []


def test_a_path_that_is_no_member_and_a_head_at_the_base_both_refuse(one_branch, monkeypatch):
    w = one_branch
    elsewhere = w["tmp"] / "not-a-member"
    elsewhere.mkdir()
    result = gr2(w["author"], monkeypatch, "pr", "create", "--repo-path", elsewhere, "--base", "main")
    assert result.exit_code == 2 and "not a member checkout" in result.output and "alpha" in result.output, result.output
    git(w["bare"], "update-ref", "refs/heads/main", w["head"])  # the base now IS the head: nothing to open
    empty = pr_create(w, monkeypatch)
    assert empty.exit_code == 2 and "nothing to open a PR for" in empty.output, empty.output
    assert w["adapter"].created == [] and pins(w) == []


def test_the_live_read_names_only_the_exact_refs_it_was_asked_for(one_branch):
    w = one_branch
    git(w["bare"], "update-ref", "refs/heads/zz/refs/heads/main", w["head"])  # a tail match of refs/heads/main
    tips = app_mod._live_remote_tips(w["author"], URL, "refs/heads/main", "refs/heads/feat/one", "refs/heads/none")
    assert tips == {"refs/heads/main": w["base"], "refs/heads/feat/one": w["head"]}


def test_repo_path_is_a_documented_option_of_pr_create():
    import typer

    opts = {o for p in typer.main.get_command(app).commands["pr"].commands["create"].params for o in p.opts}
    assert "--repo-path" in opts

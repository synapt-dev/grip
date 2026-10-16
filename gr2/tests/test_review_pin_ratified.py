"""`gr2 review pin --ratified` names a REAL receipt. The receipt is an earlier pin in this workspace that
pinned the same member key at the same head and carries at least one stamp; anything else refuses before any write
(ratified_receipt_unknown / ratified_receipt_not_covering). An accepted receipt is recorded in the bind's message
(Ratified-by:), so it is part of the gr id and `review show` reports it. A receipt bypasses only
head_already_on_remote: a remote branch past the head is still judged."""
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import next_steps
from gr2.python_cli.app import app

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_approvals import approve_as, git, world  # noqa: E402,F401

runner = CliRunner()


def binds(world):
    out = git(world["workspace"], "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/__reviews__/")
    return sorted(out.splitlines())


def only_at_a_tag(world, head=None):
    """The head leaves every branch on the remote and stays at a tag: present, but not as a branch tip."""
    remote = world["remote"]
    git(remote, "update-ref", "refs/tags/shipped", head or world["head"])
    git(remote, "update-ref", "-d", "refs/heads/feature")


def pin(world, *extra, head=None, title="second look", key="member"):
    git(world["workspace"], "config", "user.name", "Author A")  # the pin's author, never the stamper
    rows = [{"key": key, "path": "member", "remote": str(world["remote"]), "base": world["base"],
             "head": head or world["head"], "ref": "refs/heads/main", "source": str(world["member"]),
             "title": title}]
    rows_file = world["workspace"].parent / "rows-ratified.json"
    rows_file.write_text(json.dumps(rows))
    return runner.invoke(app, ["review", "pin", "--rows-json", str(rows_file), *extra])


def new_id(result):
    found = [w for w in result.stdout.split() if w.startswith("gr:")]
    assert result.exit_code == 0 and found, result.output
    return found[-1]


def test_r1_a_made_up_receipt_refuses_unknown_and_writes_nothing(world):
    assert approve_as(world, "Approver B").exit_code == 0
    only_at_a_tag(world)
    before = binds(world)
    result = pin(world, "--ratified", "nonsense")
    assert result.exit_code == 2, result.output
    assert "ratified_receipt_unknown" in result.output, result.output
    assert binds(world) == before


def test_r2_a_receipt_for_a_different_head_refuses_not_covering(world):
    member = world["member"]
    (member / "payload").write_text("later\n")
    git(member, "commit", "-am", "later")
    later = git(member, "rev-parse", "HEAD")
    other = new_id(pin(world, head=later, title="the later head"))  # pinned before any push of `later`
    only_at_a_tag(world)
    before = binds(world)
    result = pin(world, "--ratified", other)
    assert result.exit_code == 2, result.output
    assert "ratified_receipt_not_covering" in result.output and f"at {world['head']}" in result.output, result.output
    assert binds(world) == before


def test_r3_an_unstamped_pin_of_the_same_head_refuses_not_covering(world):
    only_at_a_tag(world)
    before = binds(world)
    result = pin(world, "--ratified", "gr:" + world["rid"])
    assert result.exit_code == 2, result.output
    assert "ratified_receipt_not_covering" in result.output and "no stamp" in result.output, result.output
    assert binds(world) == before


def test_r4_a_stamped_pin_of_the_same_head_is_the_receipt_and_is_recorded(world):
    assert approve_as(world, "Approver B").exit_code == 0
    only_at_a_tag(world)
    assert pin(world).exit_code == 2  # control: without the receipt the head on the remote refuses
    rid = new_id(pin(world, "--ratified", "gr:" + world["rid"]))
    shown = runner.invoke(app, ["review", "show", rid, "--json"])
    assert shown.exit_code == 0, shown.output
    [m] = json.loads(shown.stdout)["members"]
    assert m["ratified_by"] == "gr:" + world["rid"], m
    message = git(world["workspace"], "log", "-1", "--format=%B", rid.removeprefix("gr:"))
    assert f"Ratified-by: member gr:{world['rid']}" in message, message


def test_r5_the_next_line_names_a_gr_id_and_no_destructive_step():
    for line in (next_steps.head_already_on_remote(),):
        assert "--ratified gr:<id>" in line and "stamped" in line, line
        assert "--delete" not in line and "--force" not in line and "push -f" not in line, line


def test_r6_a_receipt_does_not_skip_the_judge_of_a_remote_branch_past_the_head(world):
    assert approve_as(world, "Approver B").exit_code == 0
    only_at_a_tag(world)
    other = world["workspace"].parent / "other"
    git(world["workspace"].parent, "clone", "-q", str(world["remote"]), str(other))
    git(other, "checkout", "-q", "-b", "feature", world["head"])
    (other / "more").write_text("past the head\n")
    git(other, "add", "more")
    git(other, "commit", "-m", "past the head")
    git(other, "push", "-q", "origin", "feature")
    before = binds(world)
    result = pin(world, "--ratified", "gr:" + world["rid"])
    assert result.exit_code == 2, result.output
    assert "head_behind_remote_branch" in result.output, result.output
    assert binds(world) == before


def test_r7_a_receipt_for_another_member_key_at_the_same_head_refuses_not_covering(world):
    # The receipt pinned key `member` at this head; a row keyed `other` at the same bytes is not covered by it.
    assert approve_as(world, "Approver B").exit_code == 0
    only_at_a_tag(world)
    before = binds(world)
    result = pin(world, "--ratified", "gr:" + world["rid"], key="other")
    assert result.exit_code == 2, result.output
    assert "ratified_receipt_not_covering" in result.output and "no member other at" in result.output, result.output
    assert binds(world) == before

"""The help text names the one flow that lands a reviewed change, and the review commands print their next step once."""
from __future__ import annotations

import json
import re
import shlex
import sys

from typer.testing import CliRunner

from gr2.python_cli import check_records
from gr2.python_cli.app import app
from tests.test_review_update import _env, conflict, git, move_main, two  # noqa: F401  (fixtures: real git, bare local remotes)

runner = CliRunner()


def _plain(text: str) -> str:
    # CI forces colour and rich wraps panels; the words are what is under test.
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", text).replace("│", " ").split())


def test_top_level_help_names_the_flow_that_lands_a_reviewed_change():
    out = runner.invoke(app, ["--help"])
    assert out.exit_code == 0, out.output
    assert ("To land a reviewed change: gr2 review pin, publish, stamp, gr2 check, then gr2 review merge; "
            "the steps are in gr2 review --help.") in _plain(out.stdout), out.stdout


def test_review_help_lets_publish_push_the_heads_and_check_set_alone_satisfy_merge():
    out = runner.invoke(app, ["review", "--help"])
    assert out.exit_code == 0, out.output
    text = _plain(out.stdout)
    assert "push the head" not in text, out.stdout
    assert "five steps" in text and "2. gr2 review publish push every member's pinned head to its own remote" in text
    assert ("a review over several members needs only gr2 check set, one command over every member: merge accepts on "
            "the set alone") in text, out.stdout


def test_merge_on_a_moved_base_prints_its_next_line_on_stderr_once(two):
    for name, m in two["members"].items():
        move_main(two, name)
        check_records.run_check(m["repo"], str(m["remote"]), m["head"], "test", [sys.executable, "-c", "pass"])
    out = runner.invoke(app, ["review", "merge", str(two["author"]), two["review"], "--approvals", "0"])
    assert out.exit_code == 3, out.output
    nexts = [line for line in out.stderr.splitlines() if line.startswith("next: ")]
    assert nexts == [f"next: the target moved after the pin; bring every member up to it and pin again: "
                     f"gr2 review update {shlex.quote(str(two['author']))} {two['review']} --into main"], out.stderr


def test_the_update_after_a_resolved_conflict_prints_one_bare_publish(two):
    repos = [conflict(two, name) for name in two["members"]]
    first = runner.invoke(app, ["review", "update", str(two["author"]), two["review"]])
    assert first.exit_code == 3, first.output
    for repo in repos:
        (repo / "shared.txt").write_text("both\n")
        git(repo, "commit", "-am", "resolved")
    second = runner.invoke(app, ["review", "update", str(two["author"]), two["review"]])
    assert second.exit_code == 0, second.output
    new = json.loads(second.stdout)["new_review"]
    nexts = [line for line in second.stderr.splitlines() if line.startswith("next: ")]
    assert nexts == [f"next: gr2 review publish {shlex.quote(str(two['author']))} {new}"], second.stderr
    assert runner.invoke(app, ["review", "publish", str(two["author"]), new]).exit_code == 0, "it runs as printed"

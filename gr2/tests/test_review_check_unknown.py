"""`review check` never reports `satisfied: true` for a repo or PR it knows nothing about.

With no policy for a repo, 0 required >= 0 actual made ANY repo and ANY number satisfied. A repo nothing names is
refused; a declared repo's PR that no review lane, PR group or policy names is reported as not satisfied, with the
basis named. Rows go red if the repo check, the PR basis or the policy carve-out is dropped, if the refusal leaves
stdout, or if a subdirectory run stops resolving the workspace.
"""
from __future__ import annotations

import json

from gr2.python_cli.app import app
from tests.test_review_approvals import approve_as, runner, world  # noqa: F401
from tests.test_review_check_name import _workspace


def _refused(result, code, repo, number):
    assert result.exit_code == 2, result.output
    doc = json.loads(result.stdout)
    assert doc == {"refused": code, "repo": repo, "pr_number": number}, doc
    assert "satisfied" not in result.stdout
    assert code in result.stderr and result.stderr.count("\n") == 1, result.stderr


def _unnamed(result, repo, number):
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert (doc["repo"], doc["pr_number"], doc["satisfied"]) == (repo, number, False), doc
    assert doc["basis"] == "no_lane_or_group_names_this_pr", doc


def test_unknown_repo_is_refused_not_satisfied(world):
    _refused(runner.invoke(app, ["review", "check", "nope", "999", "--json"]), "unknown_repo", "nope", 999)


def test_known_repo_with_an_unnamed_pr_is_not_satisfied(world):
    _unnamed(runner.invoke(app, ["review", "check", "member", "999", "--json"]), "member", 999)


def test_a_stamp_without_a_lane_or_pr_group_does_not_satisfy_the_pr(world):
    assert approve_as(world, "Approver B").exit_code == 0
    _unnamed(runner.invoke(app, ["review", "check", "member", "1", "--json"]), "member", 1)


def test_policy_and_lanes_keep_the_counted_report(tmp_path):
    for reviewers, satisfied in [(0, False), (1, True)]:
        ws = _workspace(tmp_path / str(reviewers), reviewers)
        result = runner.invoke(app, ["review", "check", str(ws), "alpha", "7", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert (doc["required_reviewers"], doc["actual_reviewers"], doc["satisfied"]) == (1, reviewers, satisfied)


def test_a_subdirectory_run_answers_the_same(world, monkeypatch):
    monkeypatch.chdir(world["member"])
    _refused(runner.invoke(app, ["review", "check", "nope", "999", "--json"]), "unknown_repo", "nope", 999)
    _unnamed(runner.invoke(app, ["review", "check", "member", "999"]), "member", 999)


def test_the_refusal_is_one_json_document_on_stdout_and_prose_on_stderr(world):
    result = runner.invoke(app, ["review", "check", "nope", "999"])
    assert json.loads(result.stdout)["refused"] == "unknown_repo"
    assert not result.stderr.lstrip().startswith("{") and "nope" in result.stderr, result.stderr


def test_a_pr_group_naming_the_pr_keeps_the_report(world):
    groups = world["workspace"] / ".grip" / "pr_groups"
    groups.mkdir(parents=True, exist_ok=True)
    (groups / "g.json").write_text(json.dumps({"pr_group_id": "g", "prs": [{"repo": "member", "pr_number": 5}]}))
    result = runner.invoke(app, ["review", "check", "member", "5", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["satisfied"] is True and "basis" not in doc, doc


def test_a_pr_group_alone_makes_its_repo_known(world):
    groups = world["workspace"] / ".grip" / "pr_groups"
    groups.mkdir(parents=True, exist_ok=True)
    (groups / "g.json").write_text(json.dumps({"pr_group_id": "g", "prs": [{"repo": "ghost", "pr_number": 5}]}))
    result = runner.invoke(app, ["review", "check", "ghost", "5", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["satisfied"] is True

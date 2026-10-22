"""A recorded PR group that holds a closed PR: rows R1-R6 of the design note's section 3."""

from __future__ import annotations

import json

import pytest

from gr2.python_cli import pr as pr_ops
from gr2.python_cli.platform import PRRef, PRStatus
from tests.test_pr_review_subject import KEYS, URL, _open_prs_by_head, git, gr2, reviewed  # noqa: F401


def _state(adapter, repo, number, state):
    adapter.set_status(repo, number, PRStatus(ref=PRRef(repo=repo, number=number), state=state, checks=[]))


def _reads(adapter):
    """Record every PR state read the merge or create path makes."""
    seen = []
    real = adapter.pr_status
    adapter.pr_status = lambda repo, number, **kw: seen.append((repo, number)) or real(repo, number, **kw)
    return seen


def _groups(reviewed):
    return [json.loads(p.read_text()) for p in (reviewed["author"] / ".grip" / "pr_groups").glob("*.json")]


@pytest.fixture
def adopted(reviewed, monkeypatch):
    """A group gr2 did not open: one merge adopts PRs 40 and 41 by their reviewed heads and records them.
    The adopting merge is the real one, refused by a closed 41, so the record exists and nothing merged."""
    reviewed["adapter"].pr_for_head = _open_prs_by_head(reviewed)
    beta = reviewed["repos"][KEYS[1]]
    _state(reviewed["adapter"], beta, 41, "CLOSED")
    first = gr2(reviewed["author"], monkeypatch, "pr", "merge", "--json")
    assert first.exit_code != 0, first.output
    assert {p["pr_number"] for p in _groups(reviewed)[0]["prs"]} == {40, 41}
    assert reviewed["adapter"].merged == []
    _state(reviewed["adapter"], beta, 41, "OPEN")
    reviewed["numbers"] = {reviewed["repos"][k]: 40 + i for i, k in enumerate(KEYS)}
    # The pin pass fetches refs/pull/<n>/head from https://github.com/o/<k>.git: each bare carries the PR
    # ref at the reviewed head, and the rewrite to the bare lives only in this test's isolated global config.
    config = reviewed["bare"][KEYS[0]].parent / "identity-gitconfig"
    for i, k in enumerate(KEYS):
        git(reviewed["author"] / k, "push", reviewed["bare"][k], f"{reviewed['heads'][k]}:refs/pull/{40 + i}/head")
        git(reviewed["author"], "config", "--file", config, f"url.file://{reviewed['bare'][k]}.insteadOf", URL[k])
    return reviewed


def _merge(adopted, monkeypatch):
    """The real merge_pr_group; `seen["expected_heads"]` is each head the adapter's merge was pinned to."""
    seen = {}
    adapter = adopted["adapter"]
    real = adapter.merge_pr
    adapter.merge_pr = lambda repo, number, **kw: seen.setdefault("expected_heads", {}).update({repo: kw.get("expected_head")}) or real(repo, number, **kw)
    return gr2(adopted["author"], monkeypatch, "pr", "merge", "--json"), seen


def test_r1_closed_then_reopened_merges(adopted, monkeypatch):
    "R1: the recorded PR is closed, then reopened at the same commit; merge lands it"
    alpha = adopted["repos"]["alpha"]
    _state(adopted["adapter"], alpha, 40, "OPEN")  # closed, then reopened: the host now says OPEN
    reads = _reads(adopted["adapter"])
    merged, seen = _merge(adopted, monkeypatch)
    assert merged.exit_code == 0, merged.output
    assert seen["expected_heads"] == {adopted["repos"][k]: adopted["heads"][k] for k in KEYS}
    assert (alpha, 40) in reads, "merge must read the recorded PR's state before landing it"


def test_r2_closed_with_a_different_pr_open_is_refused_naming_both(adopted, monkeypatch):
    "R2: merge refuses, exit non-zero, and the message names BOTH numbers; no merge call reaches the forge fake"
    alpha = adopted["repos"]["alpha"]
    _state(adopted["adapter"], alpha, 40, "CLOSED")
    heads = {adopted["repos"][k]: adopted["heads"][k] for k in KEYS}
    adopted["adapter"].pr_for_head = lambda repo, sha: [{"number": 50, "state": "open", "head": sha}] if repo == alpha and heads[repo] == sha else []
    merged, seen = _merge(adopted, monkeypatch)
    text = str(merged.exception) + merged.output
    assert merged.exit_code != 0, merged.output
    assert "40" in text and "50" in text, text
    assert seen == {} and adopted["adapter"].merged == []
    assert {p["pr_number"] for p in _groups(adopted)[0]["prs"]} == {40, 41}, "the record must not be rewritten"


def test_r3_closed_with_none_open_is_refused_naming_it(adopted, monkeypatch):
    "R3: merge refuses naming the closed number"
    _state(adopted["adapter"], adopted["repos"]["alpha"], 40, "CLOSED")
    adopted["adapter"].pr_for_head = lambda repo, sha: []
    merged, seen = _merge(adopted, monkeypatch)
    text = str(merged.exception) + merged.output
    assert merged.exit_code != 0, merged.output
    assert "40" in text, text
    assert seen == {} and adopted["adapter"].merged == []


def test_r4_self_opened_group_all_closed_is_replaced(reviewed, monkeypatch):
    "R4: `gr2 pr create` opens a new group that replaces the old record"
    made = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert made.exit_code == 0, made.output
    old = {(p["repo"], p["pr_number"]) for p in _groups(reviewed)[0]["prs"]}
    for repo, number in old:
        _state(reviewed["adapter"], repo, number, "CLOSED")
    again = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert again.exit_code == 0, again.output
    groups = _groups(reviewed)
    assert len(groups) == 1, "the new group replaces the old record"
    new = {(p["repo"], p["pr_number"]) for p in groups[0]["prs"]}
    assert new.isdisjoint(old) and len(new) == len(KEYS)
    assert len(reviewed["adapter"].created) == 2 * len(KEYS)


def test_r5_self_opened_group_one_still_open_still_refuses(reviewed, monkeypatch):
    "R5: `gr2 pr create` still refuses"
    made = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert made.exit_code == 0, made.output
    old = sorted((p["repo"], p["pr_number"]) for p in _groups(reviewed)[0]["prs"])
    _state(reviewed["adapter"], *old[0], "CLOSED")
    reads = _reads(reviewed["adapter"])
    again = gr2(reviewed["author"], monkeypatch, "pr", "create", "--json")
    assert again.exit_code != 0, again.output
    assert len(reviewed["adapter"].created) == len(KEYS)
    assert {(p["repo"], p["pr_number"]) for p in _groups(reviewed)[0]["prs"]} == set(old)
    # The refusal must come from reading the group's states, not from the unconditional refusal on dev.
    assert set(old) <= set(reads), "create must read each recorded PR's state before refusing"


def test_r6_control_all_open_merges_as_today_with_one_state_read(adopted, monkeypatch):
    "R6: an all-open group merges exactly as today, one extra state read and nothing else"
    reads = _reads(adopted["adapter"])
    merged, seen = _merge(adopted, monkeypatch)
    assert merged.exit_code == 0, merged.output
    assert seen["expected_heads"] == {adopted["repos"][k]: adopted["heads"][k] for k in KEYS}
    assert sorted(reads) == sorted(adopted["numbers"].items()), "one state read per recorded PR"
    assert adopted["adapter"].created == [] and len(_groups(adopted)) == 1


def test_r7_merged_member_is_not_closed(adopted, monkeypatch):
    "R7: a merged member is not closed: create still refuses while the group exists, and a merge resume is not refused as closed"
    _state(adopted["adapter"], adopted["repos"]["alpha"], 40, "MERGED")
    old = {(p["repo"], p["pr_number"]) for p in _groups(adopted)[0]["prs"]}
    again = gr2(adopted["author"], monkeypatch, "pr", "create", "--json")
    assert again.exit_code != 0, again.output
    assert "already exists" in str(again.exception) + again.output
    assert adopted["adapter"].created == []
    assert [{(p["repo"], p["pr_number"]) for p in g["prs"]} for g in _groups(adopted)] == [old], "the record must be kept"
    raised = []
    real = pr_ops.ClosedMembersError.__init__
    monkeypatch.setattr(pr_ops.ClosedMembersError, "__init__", lambda self, *a, **kw: raised.append(a) or real(self, *a, **kw))
    merged, _ = _merge(adopted, monkeypatch)
    assert raised == [], f"a merged member was refused as closed: {raised} {merged.output}"

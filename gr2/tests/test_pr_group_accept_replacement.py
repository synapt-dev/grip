"""`gr2 pr merge --accept-replacement`: the design note's case 3(b), rows A1-A9.

A recorded member is closed and exactly one other open PR sits at its reviewed head, on the same base. The flag
lands that PR in its place: the pin pass reads the replacement's host head first, and the record is rewritten (same
shape, the member's number and url) only when the merge goes ahead. Every refusal leaves the record byte-identical.
"""

from __future__ import annotations

import json
import shlex

import pytest

from gr2.python_cli.platform import AdapterError, PRRef, PRStatus
from tests.test_pr_group_closed_pr import adopted  # noqa: F401
from tests.test_pr_review_subject import KEYS, git, gr2, reviewed  # noqa: F401


def _closed(adapter, repo, number, base="main"):
    adapter.set_status(repo, number, PRStatus(ref=PRRef(repo=repo, number=number, base_branch=base), state="CLOSED", checks=[]))


def _record(adopted):
    paths = list((adopted["author"] / ".grip" / "pr_groups").glob("*.json"))
    assert len(paths) == 1, paths
    return paths[0]


def _at_head(adopted, repo, *others):
    """The host's open PRs at `repo`'s reviewed head: `others`, as (number, base) pairs; none anywhere else."""
    heads = {adopted["repos"][k]: adopted["heads"][k] for k in KEYS}
    adopted["adapter"].pr_for_head = lambda r, sha: (
        [{"number": n, "state": "open", "head": sha, "base": b} for n, b in others] if r == repo and heads[r] == sha else []
    )


def _pull_ref(adopted, key, number, commit=None):
    """The bare carries refs/pull/<number>/head, at the reviewed head unless `commit` names another."""
    git(adopted["author"] / key, "push", "-f", adopted["bare"][key], f"{commit or adopted['heads'][key]}:refs/pull/{number}/head")


def _merge(adopted, monkeypatch, *flags):
    return gr2(adopted["author"], monkeypatch, "pr", "merge", "--json", *flags)


def _unreadable(merged, replaced):
    assert merged.exit_code == 5, merged.output
    payload = json.loads(merged.output[merged.output.index("{"):])
    assert payload["status"] == "refused_unreadable", payload
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


@pytest.fixture
def replaced(adopted):
    """Case 3(b): alpha's #40 is closed and #90 is open at alpha's reviewed head, on the same base."""
    alpha = adopted["repos"]["alpha"]
    _closed(adopted["adapter"], alpha, 40)
    _at_head(adopted, alpha, (90, "main"))
    _pull_ref(adopted, "alpha", 90)
    adopted["before"] = _record(adopted).read_bytes()
    return adopted


def test_a1_flag_lands_the_one_open_pr_at_the_reviewed_head(replaced, monkeypatch):
    "A1: one merge on #90 and none on #40; the record names #90 in the same shape; the merge prints the swap"
    alpha, beta = replaced["repos"]["alpha"], replaced["repos"]["beta"]
    keys_before = [sorted(p) for p in json.loads(replaced["before"])["prs"]]
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    assert merged.exit_code == 0, merged.output
    assert sorted(replaced["adapter"].merged) == sorted([(alpha, 90), (beta, 41)])
    assert (alpha, 40) not in replaced["adapter"].merged
    record = json.loads(_record(replaced).read_text())
    assert {(p["repo"], p["pr_number"]) for p in record["prs"]} == {(alpha, 90), (beta, 41)}
    assert [sorted(p) for p in record["prs"]] == keys_before, "the record keeps its shape: no new member field"
    assert "replaced" not in record
    assert f"replaced #40 with #90 ({alpha})" in merged.output


def test_a2_without_the_flag_refuses_and_prints_the_one_command_that_lands_it(replaced, monkeypatch):
    "A2: refused, record byte-identical, and the printed command, run once, lands #90"
    alpha = replaced["repos"]["alpha"]
    merged = _merge(replaced, monkeypatch)
    text = str(merged.exception) + merged.output
    assert merged.exit_code != 0, merged.output
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]
    line = [l for l in text.splitlines() if "--accept-replacement" in l]
    assert line, text
    command = shlex.split(line[-1][line[-1].index("gr2 pr merge"):])
    assert command[:3] == ["gr2", "pr", "merge"] and command[-1] == "--accept-replacement", command
    again = gr2(replaced["author"], monkeypatch, *command[1:])
    assert again.exit_code == 0, again.output
    assert (alpha, 90) in replaced["adapter"].merged and (alpha, 40) not in replaced["adapter"].merged


def test_a3_flag_with_no_open_pr_at_the_reviewed_head_refuses(replaced, monkeypatch):
    "A3: case 3(a), nothing to take: refused, 0 merges, record byte-identical"
    _at_head(replaced, replaced["repos"]["alpha"])
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    text = str(merged.exception) + merged.output
    assert merged.exit_code == 1, merged.output
    assert "40" in text, text
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


def test_a4_flag_with_two_open_prs_at_the_reviewed_head_refuses_naming_both(replaced, monkeypatch):
    "A4: case 3(c), the flag takes exactly one: refused naming #90 and #91, 0 merges, record byte-identical"
    _at_head(replaced, replaced["repos"]["alpha"], (90, "main"), (91, "main"))
    _pull_ref(replaced, "alpha", 91)
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    text = str(merged.exception) + merged.output
    assert merged.exit_code == 1, merged.output
    assert "#90" in text and "#91" in text, text
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


def test_a5_replacement_head_moved_after_the_lookup_refuses_at_the_pin_pass(replaced, monkeypatch):
    "A5: the lookup saw #90 at the reviewed head, the host head has since moved: the pin pass refuses, nothing written"
    head = replaced["heads"]["alpha"]
    # A child of the reviewed head made without moving the checkout, so only the host's PR ref changes.
    moved = git(replaced["author"] / "alpha", "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "moved after the reads")
    _pull_ref(replaced, "alpha", 90, moved)
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    assert merged.exit_code != 0, merged.output
    assert "MOVED" in merged.output, merged.output
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


def test_a6_control_all_open_with_the_flag_merges_as_today(adopted, monkeypatch):
    "A6: nothing closed: the flag changes nothing, both recorded PRs land, no swap is printed"
    merged = _merge(adopted, monkeypatch, "--accept-replacement")
    assert merged.exit_code == 0, merged.output
    assert sorted(n for _, n in adopted["adapter"].merged) == [40, 41]
    assert "replaced #" not in merged.output


def test_a7_a_lookup_that_cannot_be_asked_is_unreadable_never_empty(replaced, monkeypatch):
    "A7: the host ask fails: exit 5 refused_unreadable naming the failed ask, 0 merges, record byte-identical"

    def down(repo, sha):
        raise AdapterError("gh: HTTP 503 from api.github.com")

    replaced["adapter"].pr_for_head = down
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    assert merged.exit_code == 5, merged.output
    payload = json.loads(merged.output[merged.output.index("{"):])
    assert payload["status"] == "refused_unreadable", payload
    assert "HTTP 503" in payload["refused"][0]["reason"], payload
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


def test_a8_replacement_on_another_base_refuses_naming_both_bases(replaced, monkeypatch):
    "A8: #90 sits at the reviewed head but targets `release`, where #40 targeted `main`: refused naming both"
    _at_head(replaced, replaced["repos"]["alpha"], (90, "release"))
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    text = str(merged.exception) + merged.output
    assert merged.exit_code == 1, merged.output
    assert "release" in text and "main" in text, text
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


def test_a9_the_flag_needs_a_review_bound_merge(replaced, monkeypatch):
    "A9: a lane merge has no reviewed head to match a replacement by: refused before any host call"
    merged = _merge(replaced, monkeypatch, replaced["author"], "someunit", "--accept-replacement")
    text = str(merged.exception) + merged.output
    assert merged.exit_code != 0, merged.output
    assert "review" in text and "--accept-replacement" in text, text
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]


def test_a10_a_partial_merge_records_the_replacement_that_landed(replaced, monkeypatch):
    "A10: #90 lands, then beta's merge fails: exit 1, the record names alpha #90 (not the closed #40), partially_merged"
    alpha, beta = replaced["repos"]["alpha"], replaced["repos"]["beta"]
    replaced["adapter"].set_fail_merge(beta, 41)
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    assert merged.exit_code == 1, merged.output
    assert replaced["adapter"].merged == [(alpha, 90)]
    record = json.loads(_record(replaced).read_text())
    assert {(p["repo"], p["pr_number"]) for p in record["prs"]} == {(alpha, 90), (beta, 41)}, record["prs"]
    assert record["group_state"] == "partially_merged"
    assert [c["pr_number"] for c in record["completed"] if c["repo"] == alpha] == [90], record["completed"]


def test_a11_a_replacement_with_no_base_is_unreadable_never_a_match(replaced, monkeypatch):
    "A11: #90 sits at the reviewed head with no base reported: exit 5 refused_unreadable, 0 merges, record byte-identical"
    _at_head(replaced, replaced["repos"]["alpha"], (90, None))
    _unreadable(_merge(replaced, monkeypatch, "--accept-replacement"), replaced)


def test_a12_a_closed_pr_base_that_cannot_be_read_is_unreadable_never_empty(replaced, monkeypatch):
    "A12: reading #40's base fails: exit 5 refused_unreadable naming the failed read, 0 merges, record byte-identical"
    real = replaced["adapter"].pr_status
    calls = []

    def status(repo, number, **kw):
        if number == 40:
            calls.append(1)
            if len(calls) == 2:
                raise AdapterError("gh: HTTP 503 reading base")
        return real(repo, number, **kw)

    replaced["adapter"].pr_status = status
    merged = _merge(replaced, monkeypatch, "--accept-replacement")
    assert len(calls) == 2, ("base read not reached", calls, merged.output)
    _unreadable(merged, replaced)
    assert "HTTP 503 reading base" in merged.output, merged.output


def test_a13_the_flag_is_refused_beside_head(replaced, monkeypatch):
    "A13: --head pins no review to match a replacement by: refused naming the flag, 0 merges, record byte-identical"
    sha = replaced["heads"]["alpha"]
    merged = _merge(replaced, monkeypatch, "--head", sha, "--accept-replacement")
    text = str(merged.exception) + merged.output
    assert merged.exit_code != 0, merged.output
    assert "--accept-replacement" in text, text
    assert replaced["adapter"].merged == []
    assert _record(replaced).read_bytes() == replaced["before"]

"""`gr2 pr view` prints the member pull requests of ONE change.

The change is the lane — a lane is the one name gr2 already groups repos under — so the verb's
members come from the LANE, not from "whatever branch each repo happens to be on" — which
is what gr1's ``pr view`` does, and the flip posture is exactly the move away from it.

Every gh call in these tests goes to a stub on disk invoked as a real subprocess, in the
house style of ``test_pr_group_legibility.py``: a stub injected past ``subprocess.run``
would prove the parse and not the call.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli.platform import GitHubAdapter

_HEAD = "a" * 40


def _detail(
    *,
    number: int,
    title: str,
    url: str,
    head: str,
    base: str,
    state: str = "OPEN",
    mergeable: str = "MERGEABLE",
) -> dict:
    return {
        "number": number,
        "url": url,
        "title": title,
        "body": f"body of {number}",
        "state": state,
        "isDraft": False,
        "mergedAt": None,
        "mergeable": mergeable,
        "headRefName": head,
        "baseRefName": base,
        "headRefOid": _HEAD,
        "author": {"login": "fathom"},
        "labels": [{"name": "enhancement"}],
        "reviewDecision": "APPROVED",
        "reviews": [{"author": {"login": "sentinel"}, "state": "APPROVED"}],
        "createdAt": "2026-09-30T00:00:00Z",
        "updatedAt": "2026-09-30T00:00:00Z",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "u"}
        ],
    }


def _gh_stub(tmp_path: Path, *, details: dict[str, dict], lists: dict[str, list]) -> str:
    """A `gh` on disk that answers `pr view` and `pr list` from two dicts.

    It also records every argv it was handed, so a test can assert on the CALL and not
    only on the parse.
    """
    spec = tmp_path / "gh-spec.json"
    spec.write_text(json.dumps({"details": details, "lists": lists}))
    argv_log = tmp_path / "gh-argv.txt"
    stub = tmp_path / "gh"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"SPEC = json.load(open({json.dumps(str(spec))}))\n"
        f"LOG = {json.dumps(str(argv_log))}\n"
        "args = sys.argv[1:]\n"
        "with open(LOG, 'a') as fh:\n"
        "    fh.write(' '.join(args) + '\\n')\n"
        "def opt(name):\n"
        "    return args[args.index(name) + 1]\n"
        "if args[:2] == ['pr', 'view']:\n"
        "    repo = opt('--repo')\n"
        "    print(json.dumps(SPEC['details'][repo]))\n"
        "elif args[:2] == ['pr', 'list']:\n"
        "    repo = opt('--repo')\n"
        "    print(json.dumps(SPEC['lists'].get(repo, [])))\n"
        "else:\n"
        "    sys.stderr.write('unexpected gh call: ' + ' '.join(args))\n"
        "    sys.exit(2)\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return str(stub)


def _use_stub(monkeypatch, stub: str) -> None:
    """Put the stub's directory first on PATH, so the adapter's bare `gh` resolves to it."""
    monkeypatch.setenv("PATH", f"{Path(stub).parent}{os.pathsep}{os.environ['PATH']}")


def _group_workspace(tmp_path: Path, *, members: list[tuple[str, int]]) -> Path:
    ws = tmp_path / "ws"
    (ws / ".grip" / "pr_groups").mkdir(parents=True)
    (ws / ".grip" / "pr_groups" / "pg_view.json").write_text(
        json.dumps(
            {
                "pr_group_id": "pg_view",
                "owner_unit": "fathom",
                "lane_name": "pr-view-witness",
                "platform": "github",
                "prs": [
                    {"repo": repo, "pr_number": number, "url": f"https://x/{repo}/pull/{number}"}
                    for repo, number in members
                ],
            }
        )
    )
    return ws


def _lane_workspace(tmp_path: Path, *, repos: list[str], branch: str = "fathom/slice") -> Path:
    """A workspace with a lane record and NO pr group record."""
    ws = tmp_path / "ws"
    lane_dir = ws / ".grip" / "state" / "lanes" / "fathom" / "pr-view-witness"
    lane_dir.mkdir(parents=True)
    (lane_dir / "lane.toml").write_text(
        "schema_version = 1\n"
        'lane_name = "pr-view-witness"\n'
        'owner_unit = "fathom"\n'
        'lane_type = "feature"\n'
        'lane_kind = "materialized"\n'
        'creation_source = "manual"\n'
        f"repos = [{', '.join(json.dumps(r) for r in repos)}]\n"
        "shared_with = []\n"
        "\n"
        "[branch_map]\n"
        + "".join(f"{r} = {json.dumps(branch)}\n" for r in repos)
    )
    return ws


def test_pr_view_prints_one_block_per_member_of_the_change(monkeypatch, tmp_path: Path) -> None:
    ws = _group_workspace(tmp_path, members=[("synapt-dev/grip", 11), ("synapt-dev/recall", 22)])
    stub = _gh_stub(
        tmp_path,
        details={
            "synapt-dev/grip": _detail(
                number=11, title="slice: pr view", url="https://x/grip/pull/11",
                head="fathom/slice", base="dev",
            ),
            "synapt-dev/recall": _detail(
                number=22, title="slice: config half", url="https://x/recall/pull/22",
                head="fathom/slice", base="main",
            ),
        },
        lists={},
    )
    _use_stub(monkeypatch, stub)

    res = CliRunner().invoke(
        gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness"]
    )

    assert res.exit_code == 0, res.output
    assert "synapt-dev/grip" in res.output and "#11" in res.output
    assert "synapt-dev/recall" in res.output and "#22" in res.output
    assert "slice: pr view" in res.output


def test_pr_view_json_carries_the_same_members_as_the_default(monkeypatch, tmp_path: Path) -> None:
    ws = _group_workspace(tmp_path, members=[("synapt-dev/grip", 11), ("synapt-dev/recall", 22)])
    stub = _gh_stub(
        tmp_path,
        details={
            "synapt-dev/grip": _detail(
                number=11, title="t11", url="https://x/grip/pull/11", head="h", base="dev"
            ),
            "synapt-dev/recall": _detail(
                number=22, title="t22", url="https://x/recall/pull/22", head="h", base="main"
            ),
        },
        lists={},
    )
    _use_stub(monkeypatch, stub)
    runner = CliRunner()

    plain = runner.invoke(gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness"])
    as_json = runner.invoke(
        gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness", "--json"]
    )

    assert plain.exit_code == 0, plain.output
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    members = payload["members"]
    assert {row["number"] for row in members} == {11, 22}
    # The human form is a DIFFERENT rendering of the same change, not a different read --
    # asserted as a MEASUREMENT, not as an inequality. An inequality is satisfied by any
    # difference at all, so it cannot fail for the reason it claims. Counting the block
    # names against the JSON rows pins the relation: one block per row, same repo, same
    # number. (Count block LINES, not "===" substrings: a block header holds the marker
    # twice, so a substring count reports double.)
    assert plain.output.strip() != as_json.output.strip()
    blocks = [line for line in plain.output.splitlines() if line.startswith("=== ")]
    assert blocks == [f"=== {row['repo']} #{row['number']} ===" for row in members]


def test_pr_view_names_a_member_with_no_pr_instead_of_dropping_it(
    monkeypatch, tmp_path: Path
) -> None:
    ws = _group_workspace(tmp_path, members=[("synapt-dev/grip", 11), ("synapt-dev/recall", 22)])
    stub = _gh_stub(
        tmp_path,
        details={
            "synapt-dev/grip": _detail(
                number=11, title="t11", url="https://x/grip/pull/11", head="h", base="dev"
            )
        },
        lists={},
    )
    _use_stub(monkeypatch, stub)

    res = CliRunner().invoke(
        gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness"]
    )

    assert res.exit_code == 0, res.output
    assert "synapt-dev/grip" in res.output
    assert "synapt-dev/recall" in res.output, "a member whose read failed must still be named"
    assert "no PR" in res.output or "unread" in res.output


def test_pr_view_falls_back_to_the_lane_record_when_no_group_exists(
    monkeypatch, tmp_path: Path
) -> None:
    ws = _lane_workspace(tmp_path, repos=["alpha", "beta"])
    stub = _gh_stub(
        tmp_path,
        details={
            "alpha": _detail(number=7, title="t7", url="https://x/alpha/pull/7", head="fathom/slice", base="dev"),
            "beta": _detail(number=8, title="t8", url="https://x/beta/pull/8", head="fathom/slice", base="main"),
        },
        lists={
            "alpha": [
                {"number": 7, "url": "https://x/alpha/pull/7", "headRefName": "fathom/slice",
                 "baseRefName": "dev", "title": "t7"}
            ],
            "beta": [
                {"number": 8, "url": "https://x/beta/pull/8", "headRefName": "fathom/slice",
                 "baseRefName": "main", "title": "t8"}
            ],
        },
    )
    _use_stub(monkeypatch, stub)

    res = CliRunner().invoke(
        gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness"]
    )

    assert res.exit_code == 0, res.output
    assert "#7" in res.output and "#8" in res.output, res.output


def test_pr_view_names_a_lane_member_whose_pr_is_not_open_yet(
    monkeypatch, tmp_path: Path
) -> None:
    """The path a stranger meets FIRST: a lane exists, its PRs do not yet."""
    ws = _lane_workspace(tmp_path, repos=["alpha", "beta"])
    stub = _gh_stub(
        tmp_path,
        details={
            "alpha": _detail(number=7, title="t7", url="https://x/alpha/pull/7", head="fathom/slice", base="dev")
        },
        lists={
            "alpha": [
                {"number": 7, "url": "https://x/alpha/pull/7", "headRefName": "fathom/slice",
                 "baseRefName": "dev", "title": "t7"}
            ],
            # beta has no PR whose head is the lane's branch.
            "beta": [],
        },
    )
    _use_stub(monkeypatch, stub)

    res = CliRunner().invoke(gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness"])

    assert res.exit_code == 0, res.output
    assert "#7" in res.output
    assert "beta" in res.output and "no open PR" in res.output, res.output


def test_pr_view_refuses_a_repo_that_is_not_a_member(monkeypatch, tmp_path: Path) -> None:
    """A filter that matched nothing must not read the same as a change with no members.

    The header says the members came from an authoritative record, so `No pull requests
    found.` under it reads as "this change is empty" rather than "you typed a name that is
    not in it" -- and a machine consumer gets `[]`, which is the same shape.
    """
    ws = _group_workspace(tmp_path, members=[("synapt-dev/grip", 11), ("synapt-dev/recall", 22)])
    stub = _gh_stub(
        tmp_path,
        details={
            "synapt-dev/grip": _detail(
                number=11, title="t11", url="https://x/grip/pull/11", head="h", base="dev"
            ),
            "synapt-dev/recall": _detail(
                number=22, title="t22", url="https://x/recall/pull/22", head="h", base="main"
            ),
        },
        lists={},
    )
    _use_stub(monkeypatch, stub)
    runner = CliRunner()

    for extra_flags in ([], ["--json"]):
        res = runner.invoke(
            gr2_app.app,
            ["pr", "view", str(ws), "fathom", "pr-view-witness", "--repo", "nosuchrepo", *extra_flags],
        )
        assert res.exit_code != 0, f"{extra_flags}: {res.output}"
        assert "nosuchrepo" in res.output, res.output
        assert "synapt-dev/grip" in res.output and "synapt-dev/recall" in res.output, (
            "the refusal must name the members it does have: " + res.output
        )


def test_pr_view_repo_filter_returns_exactly_that_member(monkeypatch, tmp_path: Path) -> None:
    """The control for the refusal above: a name that IS a member still narrows to it."""
    ws = _group_workspace(tmp_path, members=[("synapt-dev/grip", 11), ("synapt-dev/recall", 22)])
    stub = _gh_stub(
        tmp_path,
        details={
            "synapt-dev/grip": _detail(
                number=11, title="t11", url="https://x/grip/pull/11", head="h", base="dev"
            ),
            "synapt-dev/recall": _detail(
                number=22, title="t22", url="https://x/recall/pull/22", head="h", base="main"
            ),
        },
        lists={},
    )
    _use_stub(monkeypatch, stub)

    res = CliRunner().invoke(
        gr2_app.app,
        ["pr", "view", str(ws), "fathom", "pr-view-witness", "--repo", "synapt-dev/grip"],
    )

    assert res.exit_code == 0, res.output
    assert "#11" in res.output
    assert "#22" not in res.output, "the filter must exclude the other member"


def test_pr_view_empty_group_record_falls_through_to_the_lane_record(
    monkeypatch, tmp_path: Path
) -> None:
    """A record that EXISTS with no PRs is not an answer.

    Returning it gives an empty member list and never reaches the lane record, which is
    the same under-report the fallback exists to prevent -- one level in: the store is
    present, its content is empty, and it is read as the answer anyway.
    """
    ws = _lane_workspace(tmp_path, repos=["alpha", "beta"])
    groups = ws / ".grip" / "pr_groups"
    groups.mkdir(parents=True)
    (groups / "pg_empty.json").write_text(
        json.dumps(
            {
                "pr_group_id": "pg_empty",
                "owner_unit": "fathom",
                "lane_name": "pr-view-witness",
                "platform": "github",
                "prs": [],
            }
        )
    )
    stub = _gh_stub(
        tmp_path,
        details={
            "alpha": _detail(
                number=7, title="t7", url="https://x/alpha/pull/7", head="fathom/slice", base="dev"
            )
        },
        lists={
            "alpha": [
                {"number": 7, "url": "https://x/alpha/pull/7", "headRefName": "fathom/slice",
                 "baseRefName": "dev", "title": "t7"}
            ],
            "beta": [],
        },
    )
    _use_stub(monkeypatch, stub)

    res = CliRunner().invoke(gr2_app.app, ["pr", "view", str(ws), "fathom", "pr-view-witness"])

    assert res.exit_code == 0, res.output
    assert "#7" in res.output, "the empty group record must not answer for the change: " + res.output
    assert "lane record" in res.output, res.output


def test_pr_view_json_names_which_source_produced_the_members(
    monkeypatch, tmp_path: Path
) -> None:
    """The two member sources are NOT interchangeable, so the payload says which one ran.

    The group record is the authoritative member-to-PR-number map; the lane fallback
    resolves each PR by branch, so a member can come back with no number at all. A bare
    array leaves a machine consumer unable to tell which of those it is looking at --
    and the human form is told, in the header, on the same command.
    """
    runner = CliRunner()

    group_ws = _group_workspace(tmp_path / "group", members=[("synapt-dev/grip", 11)])
    stub = _gh_stub(
        tmp_path,
        details={
            "synapt-dev/grip": _detail(
                number=11, title="t11", url="https://x/grip/pull/11", head="h", base="dev"
            )
        },
        lists={},
    )
    _use_stub(monkeypatch, stub)
    from_group = runner.invoke(
        gr2_app.app, ["pr", "view", str(group_ws), "fathom", "pr-view-witness", "--json"]
    )
    assert from_group.exit_code == 0, from_group.output
    group_payload = json.loads(from_group.output)
    assert group_payload["source"] == "pr_group"
    assert group_payload["pr_group_id"] == "pg_view"

    lane_ws = _lane_workspace(tmp_path / "lane", repos=["alpha"])
    (tmp_path / "lane-stub").mkdir(parents=True, exist_ok=True)
    stub2 = _gh_stub(
        tmp_path / "lane-stub",
        details={
            "alpha": _detail(
                number=7, title="t7", url="https://x/alpha/pull/7", head="fathom/slice", base="dev"
            )
        },
        lists={
            "alpha": [
                {"number": 7, "url": "https://x/alpha/pull/7", "headRefName": "fathom/slice",
                 "baseRefName": "dev", "title": "t7"}
            ]
        },
    )
    _use_stub(monkeypatch, stub2)
    from_lane = runner.invoke(
        gr2_app.app, ["pr", "view", str(lane_ws), "fathom", "pr-view-witness", "--json"]
    )
    assert from_lane.exit_code == 0, from_lane.output
    lane_payload = json.loads(from_lane.output)
    assert lane_payload["source"] == "lane_record", lane_payload
    assert len(lane_payload["members"]) == 1


def test_pr_view_asks_gh_for_the_detail_fields(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(argv, **_kwargs):
        recorded.append(list(argv))
        return _Proc(json.dumps(_detail(number=11, title="t", url="u", head="h", base="dev")))

    import gr2.python_cli.platform as platform_mod

    monkeypatch.setattr(platform_mod.subprocess, "run", fake_run)
    GitHubAdapter(gh_binary="gh").pr_view("synapt-dev/grip", 11)

    argv = recorded[0]
    assert argv[:3] == ["gh", "pr", "view"]
    fields = argv[argv.index("--json") + 1]
    for needed in ("headRefOid", "author", "labels", "reviewDecision", "isDraft"):
        assert needed in fields, f"pr view must ask gh for {needed}"


def test_pr_view_maps_the_gh_payload_to_a_detail(monkeypatch, tmp_path: Path) -> None:
    payload = _detail(number=11, title="t", url="u", head="h", base="dev")
    import gr2.python_cli.platform as platform_mod

    # GitHubAdapter resolves its binary in __init__, so the stub must EXIST even though
    # _run_json is replaced: a name that resolves nowhere refuses before the parse runs.
    stub = tmp_path / "gh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(platform_mod, "_run_json", lambda *a, **k: payload)
    detail = GitHubAdapter(gh_binary=str(stub)).pr_view("synapt-dev/grip", 11)

    assert detail.number == 11
    assert detail.head_oid == _HEAD
    assert detail.author == "fathom"
    assert detail.labels == ["enhancement"]
    assert detail.review_decision == "APPROVED"
    assert detail.is_draft is False
    assert detail.mergeable == "MERGEABLE"
    assert [r.user for r in detail.reviews] == ["sentinel"]

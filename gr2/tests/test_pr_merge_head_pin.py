"""A gr2 merge must not land bytes the reads never saw.

The witness is the refusal, not the merge: a group whose member moved after the
reads must refuse BEFORE any member merges, naming both shas. A per-call check
inside the adapter cannot do that -- it refuses only the member it has reached,
so an earlier member merges first. The pre-check pass exists for that reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gr2.python_cli import platform as platform_mod
from gr2.python_cli.merge_verification import (
    CompletedMerge,
    MergeVerificationTarget,
)
from gr2.python_cli.platform import (
    AdapterError,
    MergeMethod,
    MergeReceipt,
    PRRef,
    PRStatus,
)
from gr2.python_cli.pr import PRHeadPinError, merge_pr_group

READ_SHA = "a" * 40
MOVED_SHA = "b" * 40

# What the host's ls-remote serves: the newest double's heads, or a per-repo error.
_HOST: dict[str, dict] = {"heads": {}, "raises": {}}


@pytest.fixture(autouse=True)
def _host_heads(monkeypatch: pytest.MonkeyPatch):
    """The pin pass reads the host's head, not the adapter's: serve the double's."""
    from gr2.python_cli import pr as pr_mod

    _HOST["heads"], _HOST["raises"] = {}, {}

    def host_pr_head_oid(root: Path, number: int, *, remote: str = "origin") -> str | None:
        if root.name in _HOST["raises"]:
            raise _HOST["raises"][root.name]
        return _HOST["heads"].get(root.name)

    monkeypatch.setattr(pr_mod, "host_pr_head_oid", host_pr_head_oid)
    yield


class _Pinned:
    """A two-call adapter double: pr_status for the pre-check, merge_pr for the act."""

    name = "github"

    def __init__(self, heads: dict[str, str]) -> None:
        self.heads = heads
        _HOST["heads"] = heads
        self.merged: list[str] = []
        self.pins_seen: dict[str, str | None] = {}
        self.status_calls: list[str] = []

    def pr_status(self, repo: str, number: int) -> PRStatus:
        self.status_calls.append(repo)
        return PRStatus(
            ref=PRRef(repo=repo, number=number),
            state="OPEN",
            head_oid=self.heads.get(repo),
        )

    def merge_pr(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod,
        expected_head: str | None = None,
    ) -> MergeReceipt:
        self.merged.append(repo)
        self.pins_seen[repo] = expected_head
        return MergeReceipt(
            requested=PRRef(repo=repo, number=number),
            observed=PRRef(repo=repo, number=number, url=f"observed://{repo}/{number}"),
            commit_sha=None,
            requested_method=method,
        )


def _group(workspace: Path, repos: list[str]) -> str:
    group_id = "pg_pin"
    state_dir = workspace / ".grip" / "pr_groups"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{group_id}.json").write_text(
        json.dumps(
            {
                "pr_group_id": group_id,
                "owner_unit": "test-unit",
                "lane_name": "test-lane",
                "prs": [
                    {"repo": repo, "pr_number": index + 1}
                    for index, repo in enumerate(repos)
                ],
            }
        )
    )
    return group_id


def _targets(workspace: Path, repos: list[str]) -> dict[str, MergeVerificationTarget]:
    return {
        repo: MergeVerificationTarget(
            repo_root=workspace / "repos" / repo,
            remote=f"https://example.test/{repo}.git",
        )
        for repo in repos
    }


def _merge(
    workspace: Path,
    repos: list[str],
    adapter: object,
    *,
    expected_heads: dict[str, str] | None = None,
) -> dict:
    return merge_pr_group(
        workspace_root=workspace,
        pr_group_id=_group(workspace, repos),
        adapter=adapter,  # type: ignore[arg-type]
        actor="agent:test",
        method=MergeMethod.MERGE,
        verification_targets=_targets(workspace, repos),
        report=lambda _message: None,
        expected_heads=expected_heads,
    )


# --- the group refuses, and refuses BEFORE any member merges -------------------


def test_a_moved_member_refuses_the_whole_group_before_merging_anything(
    tmp_path: Path,
) -> None:
    """The witness. 'api' is listed second, so a per-call check would already
    have merged 'app' by the time it noticed."""
    adapter = _Pinned({"app": READ_SHA, "api": MOVED_SHA})

    with pytest.raises(PRHeadPinError) as raised:
        _merge(
            tmp_path,
            ["app", "api"],
            adapter,
            expected_heads={"app": READ_SHA, "api": READ_SHA},
        )

    assert adapter.merged == [], "a moved member must not merge any part of the group"
    assert raised.value.expected == READ_SHA
    assert raised.value.actual == MOVED_SHA
    assert raised.value.repo == "api"
    assert (raised.value.status, raised.value.exit_code) == ("refused_head_moved", 3)
    assert [row["state"] for row in raised.value.members] == ["same", "MOVED"]
    # both shas are named in the message the author reads
    assert READ_SHA[:8] in str(raised.value)
    assert MOVED_SHA[:8] in str(raised.value)


def test_a_matching_pin_merges_and_reaches_the_adapter(tmp_path: Path) -> None:
    """The paired control for the refusal: same code path, pins correct."""
    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})

    _merge(
        tmp_path,
        ["app", "api"],
        adapter,
        expected_heads={"app": READ_SHA, "api": READ_SHA},
    )

    assert adapter.merged == ["app", "api"]
    assert adapter.pins_seen == {"app": READ_SHA, "api": READ_SHA}


def test_an_unpinned_member_is_refused_not_merged(tmp_path: Path) -> None:
    """There is no unpinned mode: a member with no pin refuses the group (exit 4)."""
    from gr2.python_cli.pr import PRUnpinnedError

    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})

    with pytest.raises(PRUnpinnedError) as raised:
        _merge(tmp_path, ["app", "api"], adapter, expected_heads={"app": READ_SHA})

    assert raised.value.exit_code == 4
    assert raised.value.unpinned == ["api"]
    assert adapter.merged == []
    assert adapter.status_calls == []


def test_a_pin_for_a_repo_not_in_the_group_refuses_loudly(tmp_path: Path) -> None:
    """A typo must not silently leave the real member unpinned."""
    adapter = _Pinned({"app": READ_SHA})

    with pytest.raises(ValueError) as raised:
        _merge(tmp_path, ["app"], adapter, expected_heads={"ap": READ_SHA})

    assert "ap" in str(raised.value)
    assert adapter.merged == []


def test_an_adapter_that_cannot_report_a_head_refuses_rather_than_assumes(
    tmp_path: Path,
) -> None:
    """Unverifiable is not verified, and a head the host cannot give is never moved."""
    from gr2.python_cli import pr as pr_mod

    adapter = _Pinned({})

    with pytest.raises(pr_mod.PRHeadUnreadableError) as raised:
        _merge(tmp_path, ["app"], adapter, expected_heads={"app": READ_SHA})

    assert adapter.merged == []
    assert raised.value.exit_code == 5
    assert raised.value.status == "refused_unreadable"


# --- the pin reaches gh, and the control that proves the assertion can fail ---


class _Proc:
    stderr = ""

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _adapter_argv(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> list[list[str]]:
    recorded: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        recorded.append(list(argv))
        if argv[1:3] == ["pr", "merge"]:
            return _Proc("")
        return _Proc(
            json.dumps(
                {
                    "number": 42,
                    "url": "https://github.com/owner/repo/pull/42",
                    "state": "MERGED",
                    "mergeCommit": {"oid": "a" * 40},
                }
            )
        )

    monkeypatch.setattr(platform_mod.subprocess, "run", fake_run)
    platform_mod.GitHubAdapter().merge_pr("owner/repo", 42, method=MergeMethod.MERGE, **kwargs)
    return recorded


def test_the_pin_reaches_gh_as_match_head_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _adapter_argv(monkeypatch, expected_head=READ_SHA)

    assert recorded[0][1:3] == ["pr", "merge"]
    assert "--match-head-commit" in recorded[0]
    assert recorded[0][recorded[0].index("--match-head-commit") + 1] == READ_SHA


def test_no_pin_means_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. Without this, the assertion above could pass for the wrong
    reason -- a flag always in the argv proves nothing about the pin."""
    recorded = _adapter_argv(monkeypatch)

    assert "--match-head-commit" not in recorded[0]


# --- the CLI spelling of the pin ----------------------------------------------


def _group_spec(repos: list[str]) -> dict[str, object]:
    return {"prs": [{"repo": repo, "pr_number": n + 1} for n, repo in enumerate(repos)]}


def test_a_bare_sha_binds_the_only_member() -> None:
    from gr2.python_cli.app import _parse_head_pins

    assert _parse_head_pins([READ_SHA], _group_spec(["app"])) == {"app": READ_SHA}


def test_a_bare_sha_is_refused_when_it_could_land_on_the_wrong_member() -> None:
    """Silently binding the first member would read as protection and not be it."""
    from gr2.python_cli.app import _parse_head_pins

    with pytest.raises(ValueError) as raised:
        _parse_head_pins([READ_SHA], _group_spec(["app", "api"]))

    assert "app" in str(raised.value) and "api" in str(raised.value)


def test_repo_equals_sha_binds_that_member_only() -> None:
    """Each REPO=SHA binds its own member.

    Both members are pinned here because a pin set covering part of the group is now
    refused; the property being asserted is unchanged, and a complete pin is where it
    can still be observed. The partial case has its own row above.
    """
    from gr2.python_cli.app import _parse_head_pins

    pins = _parse_head_pins(
        [f"app={READ_SHA}", f"api={MOVED_SHA}"], _group_spec(["app", "api"])
    )

    assert pins == {"app": READ_SHA, "api": MOVED_SHA}


def test_two_pins_for_one_member_must_agree() -> None:
    from gr2.python_cli.app import _parse_head_pins

    with pytest.raises(ValueError):
        _parse_head_pins([f"app={READ_SHA}", f"app={MOVED_SHA}"], _group_spec(["app"]))
    assert _parse_head_pins(
        [f"app={READ_SHA}", f"app={READ_SHA}"], _group_spec(["app"])
    ) == {"app": READ_SHA}


def test_a_pin_with_no_commit_refuses() -> None:
    from gr2.python_cli.app import _parse_head_pins

    with pytest.raises(ValueError):
        _parse_head_pins(["app="], _group_spec(["app"]))


# --- the three ways a pin can be wrong before any merge happens ----------------
# All three are parser refusals rather than merge refusals: each is decidable from
# the group and the flag alone, and a refusal at parse time reaches the operator as
# a sentence while the same fault found later reaches them as a traceback.


def test_a_pin_set_that_covers_only_part_of_the_group_refuses() -> None:
    """A pin is a claim that every member was read, so pinning one member of two
    leaves the other merging whatever it carries. The flag then reads as protection
    for the whole group while protecting half of it, which is the same hazard the
    bare-SHA refusal exists to close."""
    from gr2.python_cli.app import _parse_head_pins

    with pytest.raises(ValueError) as raised:
        _parse_head_pins([f"app={READ_SHA}"], _group_spec(["app", "api"]))

    message = str(raised.value)
    assert "api" in message, f"the refusal must name the member left unpinned: {message}"


def test_the_unpinned_members_are_named_in_the_group_s_own_order() -> None:
    """WHICH ORDER they are named in, which nothing else here pins.

    Ordering the distinct members with `sorted()` SURVIVED every coverage row this file
    had, because each of them uses a group whose members are already alphabetical -- so
    `sorted()` names the same object and prints the same sentence. The group below is
    deliberately out of order, which is the only shape where the choice is visible.
    """
    from gr2.python_cli.app import _parse_head_pins

    with pytest.raises(ValueError) as raised:
        _parse_head_pins([f"zeta={READ_SHA}"], _group_spec(["zeta", "mid", "alpha"]))

    message = str(raised.value)
    assert "mid, alpha" in message, (
        f"the unpinned members must be named in the group's own order: {message}"
    )


def test_a_group_that_lists_one_repo_twice_can_be_pinned_completely() -> None:
    """The coverage guard counted POSITIONS, so a group naming one repo twice compared
    a COMPLETE pin set against a longer list and refused -- and the message it printed
    named nobody, because `unpinned` filtered that same duplicated list. Both halves
    were wrong: the group IS fully pinned, and a refusal that names no member cannot
    be acted on."""
    from gr2.python_cli.app import _parse_head_pins

    assert _parse_head_pins([f"app={READ_SHA}"], _group_spec(["app", "app"])) == {
        "app": READ_SHA
    }


def test_an_empty_pin_set_is_refused_by_the_library(tmp_path: Path) -> None:
    """W4-LIB: no pins at all is not "no enforcement"; merge_pr_group refuses it
    itself, so a library caller cannot merge unpinned either."""
    from gr2.python_cli.pr import PRUnpinnedError

    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})

    for empty in (None, {}):
        with pytest.raises(PRUnpinnedError) as raised:
            _merge(tmp_path, ["app", "api"], adapter, expected_heads=empty)
        assert raised.value.exit_code == 4
        assert raised.value.unpinned == ["app", "api"]
    assert adapter.merged == [], "0 merge_pr calls"


def test_a_pin_that_is_not_forty_lowercase_hex_is_refused_as_a_format_problem() -> None:
    """An abbreviated or uppercase sha OF THE CORRECT HEAD used to parse, then fail
    the comparison and be reported as "the branch moved after the reads" -- true of
    the comparison and false about the cause, which sends the reader to redo a review
    that was never the problem."""
    from gr2.python_cli.app import _parse_head_pins

    for value in ("abc1234", READ_SHA.upper(), READ_SHA[:12]):
        with pytest.raises(ValueError) as raised:
            _parse_head_pins([f"app={value}"], _group_spec(["app"]))
        message = str(raised.value)
        assert "moved" not in message, f"{value!r} was reported as a moved branch: {message}"
        assert "40" in message, f"the refusal must name the required shape: {message}"


def test_a_repo_key_that_is_not_a_member_is_refused_at_parse_time() -> None:
    """A typo'd repo key used to survive the parser and reach the merge, whose error
    is not the type the CLI catches -- so the operator met a traceback rather than a
    sentence. Refusing it here keeps every fault of this shape in one place."""
    from gr2.python_cli.app import _parse_head_pins

    with pytest.raises(ValueError) as raised:
        _parse_head_pins([f"tpyp={READ_SHA}"], _group_spec(["app", "api"]))

    message = str(raised.value)
    assert "tpyp" in message, f"the refusal must name the key that is wrong: {message}"


def test_legacy_signature_refuses_whole_group_before_status_or_merge(tmp_path: Path) -> None:
    class Legacy(_Pinned):
        def merge_pr(self, repo: str, number: int, *, method: MergeMethod) -> MergeReceipt:
            self.merged.append(repo)
            raise AssertionError("legacy adapter must never be invoked")

    adapter = Legacy({"app": READ_SHA, "api": READ_SHA})
    with pytest.raises(AdapterError, match="expected_head"):
        _merge(tmp_path, ["app", "api"], adapter,
               expected_heads={"app": READ_SHA, "api": READ_SHA})
    assert adapter.status_calls == []
    assert adapter.merged == []


def test_internal_type_error_is_not_retried_without_pin(tmp_path: Path) -> None:
    class Broken(_Pinned):
        def merge_pr(self, repo: str, number: int, *, method: MergeMethod,
                     expected_head: str | None = None) -> MergeReceipt:
            self.merged.append(repo)
            self.pins_seen[repo] = expected_head
            raise TypeError("inside compatible adapter")

    adapter = Broken({"app": READ_SHA, "api": READ_SHA})
    with pytest.raises(TypeError, match="inside compatible adapter"):
        _merge(tmp_path, ["app", "api"], adapter,
               expected_heads={"app": READ_SHA, "api": READ_SHA})
    assert adapter.merged == ["app"]
    assert adapter.pins_seen == {"app": READ_SHA}


def test_a_platform_error_on_the_pin_read_refuses_unreadable_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli import pr as pr_mod
    from gr2.python_cli.review import ReviewError

    records: list[dict] = []
    monkeypatch.setattr(pr_mod, "_record_merge_failure", lambda **kw: records.append(kw))
    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})
    _HOST["raises"]["app"] = ReviewError("ls-remote: HTTP 502: bad gateway")

    with pytest.raises(pr_mod.PRHeadUnreadableError) as raised:
        _merge(tmp_path, ["app", "api"], adapter,
               expected_heads={"app": READ_SHA, "api": READ_SHA})

    assert adapter.merged == [], "no member may merge after the pin read failed"
    assert raised.value.completed == []
    assert raised.value.exit_code == 5
    assert raised.value.status == "refused_unreadable"
    assert "502" in str(raised.value)
    assert records == [], "an unreadable head is not a merge failure: nothing happened"


def test_a_non_platform_error_on_the_pin_read_is_not_swallowed(tmp_path: Path) -> None:
    """The control: the catch is no wider than ReviewError."""
    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})
    _HOST["raises"]["app"] = KeyError("head_oid")

    with pytest.raises(KeyError, match="head_oid"):
        _merge(tmp_path, ["app", "api"], adapter,
               expected_heads={"app": READ_SHA, "api": READ_SHA})

    assert adapter.merged == []


def test_an_unreadable_member_head_refuses_the_group_with_exit_5_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both pinned; B's head cannot be read. Unknown is not moved and not matching."""
    from gr2.python_cli import app as gr2_app
    from gr2.python_cli import platform as platform_ops
    from gr2.python_cli.review import ReviewError

    from .test_pr_merge_missing_target import (
        GROUP_ID, LANE_NAME, OWNER_UNIT, _run_main, _workspace,
    )

    workspace = _workspace(tmp_path, ["app", "api"])
    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})
    _HOST["raises"]["api"] = ReviewError("ls-remote failed: HTTP 502")
    monkeypatch.setattr(platform_ops, "get_platform_adapter", lambda _name: adapter)
    monkeypatch.setattr(gr2_app, "_merge_verification_targets",
                        lambda _root: _targets(workspace, ["app", "api"]))
    group_file = workspace / ".grip" / "pr_groups" / f"{GROUP_ID}.json"
    before = group_file.read_bytes()
    outbox = workspace / ".grip" / "events" / "outbox.jsonl"

    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME, "--json",
                      "--match-head-commit", f"app={READ_SHA}",
                      "--match-head-commit", f"api={READ_SHA}"])
    output = "".join(capsys.readouterr())

    assert code == 5, output
    assert adapter.merged == [], "0 merge_pr calls"
    assert json.loads(output[output.index("{"):])["status"] == "refused_unreadable", output
    assert "api" in output and "could not be read" in output, output
    events = outbox.read_text() if outbox.exists() else ""
    assert "pr.merge_failed" not in events, events
    assert group_file.read_bytes() == before, "the group file must be byte-identical"


_BOTH_PINNED = ["--match-head-commit", f"app={READ_SHA}", "--match-head-commit", f"api={READ_SHA}"]


def _cli_refusal(tmp_path, capsys, monkeypatch, heads, raises=None, pins=_BOTH_PINNED,
                 as_json=True):
    """Run `pr merge` (default: --json, both members pinned to READ_SHA); return
    (exit code, payload, adapter, group file before, group file, outbox). Without
    --json the payload is {"raw": all output}."""
    from gr2.python_cli import app as gr2_app
    from gr2.python_cli import platform as platform_ops

    from .test_pr_merge_missing_target import (
        GROUP_ID, LANE_NAME, OWNER_UNIT, _run_main, _workspace,
    )

    workspace = _workspace(tmp_path, ["app", "api"])
    adapter = _Pinned(heads)
    _HOST["raises"].update(raises or {})
    monkeypatch.setattr(platform_ops, "get_platform_adapter", lambda _name: adapter)
    monkeypatch.setattr(gr2_app, "_merge_verification_targets",
                        lambda _root: _targets(workspace, ["app", "api"]))
    group_file = workspace / ".grip" / "pr_groups" / f"{GROUP_ID}.json"
    before = group_file.read_bytes()
    outbox = workspace / ".grip" / "events" / "outbox.jsonl"
    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME,
                      *(["--json"] if as_json else []), *pins])
    out, err = capsys.readouterr()
    output = out + err
    payload = (json.loads(out[out.index("{"):]) if as_json and "{" in out
               else {"raw": output})
    payload["stderr"] = err
    return code, payload, adapter, before, group_file, outbox


def _states(payload) -> dict[str, str]:
    return {row["repo"]: row["state"] for row in payload.get("members", [])}


def test_a_moved_head_refuses_with_exit_3_and_the_table_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1: B's host head differs from its pin."""
    code, payload, adapter, before, group_file, outbox = _cli_refusal(
        tmp_path, capsys, monkeypatch, {"app": READ_SHA, "api": MOVED_SHA}
    )

    assert code == 3, payload
    assert payload["status"] == "refused_head_moved", payload
    assert adapter.merged == [], "0 merge_pr calls"
    assert group_file.read_bytes() == before, "the group file must be byte-identical"
    events = outbox.read_text() if outbox.exists() else ""
    assert "pr.merge_failed" not in events, events
    assert _states(payload) == {"app": "same", "api": "MOVED"}, payload
    api = next(row for row in payload["members"] if row["repo"] == "api")
    assert (api["pin"], api["host_head"]) == (READ_SHA, MOVED_SHA), api


def test_a_moved_first_member_does_not_stop_the_pass_before_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1-ALL: A moved, B same; the table still lists both."""
    code, payload, adapter, _before, _file, _outbox = _cli_refusal(
        tmp_path, capsys, monkeypatch, {"app": MOVED_SHA, "api": READ_SHA}
    )

    assert code == 3, payload
    assert adapter.merged == []
    assert _states(payload) == {"app": "MOVED", "api": "same"}, payload


def test_unreadable_wins_over_moved_and_the_table_names_both(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIX: A moved, B unreadable: exit 5, and the table names both."""
    from gr2.python_cli.review import ReviewError

    code, payload, adapter, before, group_file, _outbox = _cli_refusal(
        tmp_path, capsys, monkeypatch, {"app": MOVED_SHA, "api": READ_SHA},
        raises={"api": ReviewError("ls-remote failed: HTTP 502")},
    )

    assert code == 5, payload
    assert payload["status"] == "refused_unreadable", payload
    assert adapter.merged == []
    assert group_file.read_bytes() == before
    assert _states(payload) == {"app": "MOVED", "api": "UNREADABLE"}, payload


def test_the_pin_pass_reads_the_host_not_the_adapter_status(tmp_path: Path) -> None:
    """READ-SOURCE: pr_status raises, the host serves the pins: the merge succeeds."""

    class _StatusRaises(_Pinned):
        def pr_status(self, repo: str, number: int) -> PRStatus:
            raise AdapterError("pr_status must not be the head's source")

    adapter = _StatusRaises({"app": READ_SHA, "api": READ_SHA})

    _merge(tmp_path, ["app", "api"], adapter,
           expected_heads={"app": READ_SHA, "api": READ_SHA})

    assert adapter.merged == ["app", "api"]


def _assert_refused_unpinned(code, payload, adapter, before, group_file, outbox, unpinned):
    assert code == 4, payload
    assert payload["status"] == "refused_unpinned", payload
    assert adapter.merged == [], "0 merge_pr calls"
    assert group_file.read_bytes() == before, "the group file must be byte-identical"
    events = outbox.read_text() if outbox.exists() else ""
    assert "pr.merge_failed" not in events, events
    assert [r["repo"] for r in payload["members"] if r["state"] == "UNPINNED"] == unpinned
    assert all(r["number"] in (1, 2) for r in payload["members"]), payload


def test_no_pins_on_the_cli_refuses_and_names_every_member(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4: the ordinary `pr merge` with no --match-head-commit refuses, exit 4."""
    result = _cli_refusal(tmp_path, capsys, monkeypatch,
                          {"app": READ_SHA, "api": READ_SHA}, pins=[])
    _assert_refused_unpinned(*result, unpinned=["app", "api"])


def _refusal_reason(payload) -> str:
    return payload["refused"][0]["reason"]


def test_no_pins_names_the_members_with_no_reviewed_head_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zero-rows refusal uses the design's words: "no reviewed head bound for <repo>"."""
    code, payload, *_ = _cli_refusal(tmp_path, capsys, monkeypatch,
                                     {"app": READ_SHA, "api": READ_SHA}, pins=[])
    assert code == 4, payload
    assert "no reviewed head bound for app, api" in _refusal_reason(payload), payload


def test_a_partial_pin_set_refuses_unpinned(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5 partial: the member left without a pin is UNPINNED and the refusal names it.
    Nothing describes an unpinned mode: there is none, so neither "would merge unpinned"
    nor "or none" appears anywhere in the output."""
    result = _cli_refusal(tmp_path, capsys, monkeypatch, {"app": READ_SHA, "api": READ_SHA},
                          pins=["--match-head-commit", f"app={READ_SHA}"])
    _assert_refused_unpinned(*result, unpinned=["api"])
    payload = result[1]
    assert "no reviewed head bound for api" in _refusal_reason(payload), payload
    output = json.dumps(payload)
    assert "would merge unpinned" not in output, output
    assert "or none" not in output, output


def test_two_disagreeing_pins_for_one_member_refuse_ambiguous_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5 conflicting: a repo pinned twice is AMBIGUOUS and the refusal names both pins;
    the other member's valid pin still holds."""
    code, payload, adapter, before, group_file, outbox = _cli_refusal(
        tmp_path, capsys, monkeypatch, {"app": READ_SHA, "api": READ_SHA},
        pins=["--match-head-commit", f"app={READ_SHA}", "--match-head-commit", f"app={MOVED_SHA}",
              "--match-head-commit", f"api={READ_SHA}"],
    )
    assert code == 4, payload
    assert payload["status"] == "refused_unpinned", payload
    assert adapter.merged == [], "0 merge_pr calls"
    assert group_file.read_bytes() == before, "the group file must be byte-identical"
    events = outbox.read_text() if outbox.exists() else ""
    assert "pr.merge_failed" not in events, events
    assert _states(payload) == {"app": "AMBIGUOUS"}, payload
    app = next(row for row in payload["members"] if row["repo"] == "app")
    assert app["pins"] == [READ_SHA, MOVED_SHA], app
    reason = _refusal_reason(payload)
    assert f"app: two pins, {READ_SHA[:12]} and {MOVED_SHA[:12]}" in reason, reason
    assert "no reviewed head bound" not in reason, "api is pinned; nothing else is unbound"


@pytest.mark.parametrize("broken", ["pr_number_true", "pr_number_float", "no_target_for_api"])
def test_the_groups_own_refusal_comes_before_ambiguous_pins(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """A malformed member, or a member with no target, is the more basic error: with two
    pins for app it still exits through the group's own refusal, never AMBIGUOUS with a
    number nobody wrote, and never a traceback."""
    from gr2.python_cli import app as gr2_app
    from gr2.python_cli import platform as platform_ops

    from .test_pr_merge_missing_target import (
        GROUP_ID, LANE_NAME, OWNER_UNIT, _run_main, _workspace,
    )

    workspace = _workspace(tmp_path, ["app", "api"])
    group_file = workspace / ".grip" / "pr_groups" / f"{GROUP_ID}.json"
    if broken.startswith("pr_number"):
        group = json.loads(group_file.read_text())
        group["prs"][0]["pr_number"] = True if broken == "pr_number_true" else 7.9
        group_file.write_text(json.dumps(group))
    target_repos = ["app"] if broken == "no_target_for_api" else ["app", "api"]
    adapter = _Pinned({"app": READ_SHA, "api": READ_SHA})
    monkeypatch.setattr(platform_ops, "get_platform_adapter", lambda _name: adapter)
    monkeypatch.setattr(gr2_app, "_merge_verification_targets",
                        lambda _root: _targets(workspace, target_repos))

    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME, "--json",
                      "--match-head-commit", f"app={READ_SHA}",
                      "--match-head-commit", f"app={MOVED_SHA}",
                      "--match-head-commit", f"api={READ_SHA}"])
    out, err = capsys.readouterr()
    output = out + err

    assert code not in (0, 4), output
    assert "AMBIGUOUS" not in output, output
    assert "refused_unpinned" not in output, output
    assert "Traceback" not in output, output
    assert adapter.merged == [], "0 merge_pr calls"
    if broken == "no_target_for_api":
        assert "api" in output, output
    else:
        assert ("True" if broken == "pr_number_true" else "7.9") in output, output


def test_a_moved_head_prints_the_table_without_json(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEXT: the human path prints the member table, the moved row marked MOVED."""
    code, payload, adapter, *_ = _cli_refusal(
        tmp_path, capsys, monkeypatch, {"app": READ_SHA, "api": MOVED_SHA}, as_json=False
    )

    assert code == 3, payload
    assert adapter.merged == []
    lines = [line.strip() for line in payload["raw"].splitlines()]
    moved = [line for line in lines if line.startswith("api#2 ")]
    assert len(moved) == 1 and moved[0].endswith("MOVED"), payload["raw"]

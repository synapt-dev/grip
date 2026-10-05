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


class _Pinned:
    """A two-call adapter double: pr_status for the pre-check, merge_pr for the act."""

    name = "github"

    def __init__(self, heads: dict[str, str]) -> None:
        self.heads = heads
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


def test_an_unpinned_member_is_not_pre_checked(tmp_path: Path) -> None:
    """Opt-in by construction: no pin, no pr_status read, no enforcement."""
    adapter = _Pinned({"app": MOVED_SHA, "api": MOVED_SHA})

    _merge(tmp_path, ["app", "api"], adapter)

    assert adapter.merged == ["app", "api"]
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
    """Unverifiable is not verified."""
    adapter = _Pinned({})

    with pytest.raises(PRHeadPinError):
        _merge(tmp_path, ["app"], adapter, expected_heads={"app": READ_SHA})

    assert adapter.merged == []


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


def test_an_empty_pin_set_is_accepted_and_means_no_enforcement() -> None:
    """THE DEFAULT PATH, and nothing asserted it: no `--match-head-commit` at all.

    `pins and` in the coverage guard is load-bearing. Drop it and an EMPTY pin set becomes
    indistinguishable from a partial one, so a group the caller deliberately left unpinned
    is refused with "pins 0 of 2 members; ... would merge unpinned" -- a sentence about a
    fault nobody committed, on the ordinary merge. A body's Not-covered line calling this
    case unchanged is true of the code and is not a guard on it; naming the common path as
    unchanged is not the same as testing it.
    """
    from gr2.python_cli.app import _parse_head_pins

    assert _parse_head_pins(None, _group_spec(["app", "api"])) == {}
    assert _parse_head_pins([], _group_spec(["app", "api"])) == {}


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

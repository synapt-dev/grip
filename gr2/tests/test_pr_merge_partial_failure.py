from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from gr2.python_cli.merge_verification import CompletedMerge, MergeVerificationTarget
from gr2.python_cli.platform import (
    AdapterError,
    MergeEvidenceError,
    MergeMethod,
    MergeReceipt,
    PRRef,
)
from gr2.python_cli.pr import (
    PRMergeError,
    PRMergeOutcomeUnknownError,
    PRMergePostconditionError,
    PRMergeTargetError,
    merge_pr_group,
)


class _FailAfter:
    name = "github"

    def __init__(self, fail_on: str | None = None, unknown_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.unknown_on = unknown_on
        self.calls: list[str] = []

    def merge_pr(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod,
        expected_head: str | None = None,
    ) -> MergeReceipt:
        self.calls.append(repo)
        if repo == self.fail_on:
            raise AdapterError(f"simulated host refusal for {repo}")
        if repo == self.unknown_on:
            raise MergeEvidenceError(
                requested=PRRef(repo=repo, number=number),
                requested_method=method,
                reason="receipt lookup unavailable",
            )
        return MergeReceipt(
            requested=PRRef(repo=repo, number=number),
            observed=PRRef(
                repo=repo,
                number=number,
                url=f"observed://{repo}/{number}",
            ),
            commit_sha=None,
            requested_method=method,
        )


def _group(workspace: Path, repos: list[str]) -> str:
    group_id = "pg_test"
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
    adapter: _FailAfter,
) -> dict:
    return merge_pr_group(
        workspace_root=workspace,
        pr_group_id=_group(workspace, repos),
        adapter=adapter,
        actor="agent:test",
        method=MergeMethod.MERGE,
        verification_targets=_targets(workspace, repos),
        report=lambda _message: None,
    )


def test_completed_is_required_and_keyword_only() -> None:
    parameter = inspect.signature(PRMergeError.__init__).parameters["completed"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_an_empty_completed_list_is_explicit_evidence_not_missing_evidence() -> None:
    error = PRMergeError("app", 1, "boom", completed=[])

    assert error.completed == []


def test_partial_failure_carries_adapter_authored_completed_records(tmp_path: Path) -> None:
    with pytest.raises(PRMergeError) as raised:
        _merge(tmp_path, ["app", "api"], _FailAfter(fail_on="api"))

    assert raised.value.operation_acknowledged is False
    assert all(isinstance(item, CompletedMerge) for item in raised.value.completed)
    assert [item.receipt.observed.repo for item in raised.value.completed] == ["app"]
    assert [item.receipt.observed.url for item in raised.value.completed] == [
        "observed://app/1"
    ]
    assert raised.value.completed[0].parent_verdict.kind.value == "unverifiable"


def test_failure_on_first_repo_carries_an_explicit_empty_completed_list(
    tmp_path: Path,
) -> None:
    with pytest.raises(PRMergeError) as raised:
        _merge(tmp_path, ["app", "api"], _FailAfter(fail_on="app"))

    assert raised.value.completed == []


def test_receipt_failure_is_outcome_unknown_and_must_not_be_retried(tmp_path: Path) -> None:
    with pytest.raises(PRMergeOutcomeUnknownError) as raised:
        _merge(tmp_path, ["app", "api"], _FailAfter(unknown_on="api"))

    assert raised.value.operation_acknowledged is True
    assert [item.receipt.observed.repo for item in raised.value.completed] == ["app"]
    assert "do not retry" in str(raised.value)


def test_postcondition_failure_preserves_the_acknowledged_receipt(
    tmp_path: Path,
) -> None:
    adapter = _FailAfter()

    with pytest.raises(PRMergePostconditionError) as raised:
        merge_pr_group(
            workspace_root=tmp_path,
            pr_group_id=_group(tmp_path, ["app"]),
            adapter=adapter,
            actor="agent:test",
            method=MergeMethod.MERGE,
            verification_targets=_targets(tmp_path, ["app"]),
            report=lambda _message: (_ for _ in ()).throw(
                RuntimeError("report sink unavailable")
            ),
        )

    assert raised.value.operation_acknowledged is True
    assert raised.value.outcome_unknown is False
    assert raised.value.completed == []
    assert raised.value.receipt.observed.repo == "app"
    assert "do not retry" in str(raised.value)


def test_missing_verification_target_refuses_before_any_host_call(tmp_path: Path) -> None:
    adapter = _FailAfter()
    group_id = _group(tmp_path, ["app", "api"])

    # THE TYPE IS THE POINT HERE, not an incidental. This refusal used to be a bare
    # ValueError, which the merge verb's CLI does NOT catch, so it escaped main() as a
    # traceback. Pinning PRMergeTargetError is what lets this row witness the
    # sentence-reaching-the-operator claim as well as the ordering one -- Atlas
    # confirmed the MRO is PRMergeTargetError -> PRMergeError -> RuntimeError, so a
    # ValueError assertion fails for exactly this reason and nothing subtler.
    with pytest.raises(PRMergeTargetError, match="api"):
        merge_pr_group(
            workspace_root=tmp_path,
            pr_group_id=group_id,
            adapter=adapter,
            actor="agent:test",
            method=MergeMethod.MERGE,
            verification_targets=_targets(tmp_path, ["app"]),
            report=lambda _message: None,
        )

    assert adapter.calls == []


def test_success_state_is_derived_from_consumed_records(tmp_path: Path) -> None:
    result = _merge(tmp_path, ["app", "api"], _FailAfter())

    assert [item["repo"] for item in result["completed"]] == ["app", "api"]
    assert all("parent_verdict" in item for item in result["completed"])


def test_event_failure_cannot_erase_the_in_process_completed_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gr2.python_cli import pr as pr_module

    monkeypatch.setattr(
        pr_module,
        "emit",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("event sink unavailable")),
    )

    with pytest.raises(PRMergeError) as raised:
        _merge(tmp_path, ["app", "api"], _FailAfter(fail_on="api"))

    assert [item.receipt.observed.repo for item in raised.value.completed] == ["app"]

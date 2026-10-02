"""Evidence-bound parent verification for gr2 pull-request merges.

The adapter returns an immutable commit OID from the merged PR record. This
module brings that exact object into the local DAG without writing FETCH_HEAD,
counts its parents, reports the result, and only then constructs a completed
merge record. No step re-resolves the base branch or reads process-global
mutable fetch state.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .platform import MergeMethod, MergeReceipt


@dataclass(frozen=True)
class MergeVerificationTarget:
    """Explicit local evidence source for one host repository."""

    repo_root: Path
    remote: str


class ParentVerdictKind(StrEnum):
    NOT_PERFORMED = "not_performed"
    VERIFIED = "verified"
    WRONG = "wrong"
    UNVERIFIABLE = "unverifiable"
    NOT_APPLICABLE = "not_applicable"


class ParentVerdict:
    """Opaque parent-verification result.

    The public constructor deliberately claims only NOT_PERFORMED. Verified,
    wrong, unverifiable, and not-applicable results are minted by
    `verify_parent_shape`, which has the evidence needed to earn them. Python
    code in the same process can always forge an object with `object.__new__`.
    The invariant covers supported construction routes, not hostile memory
    mutation inside the verifier's process.
    """

    __slots__ = ("_commit_sha", "_detail", "_kind", "_parent_count")

    def __init__(self) -> None:
        object.__setattr__(self, "_kind", ParentVerdictKind.NOT_PERFORMED)
        object.__setattr__(self, "_commit_sha", None)
        object.__setattr__(self, "_parent_count", None)
        object.__setattr__(self, "_detail", "parent verification was not performed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ParentVerdict is immutable")

    @classmethod
    def _earned(
        cls,
        *,
        kind: ParentVerdictKind,
        commit_sha: str | None,
        parent_count: int | None,
        detail: str,
    ) -> ParentVerdict:
        value = object.__new__(cls)
        object.__setattr__(value, "_kind", kind)
        object.__setattr__(value, "_commit_sha", commit_sha)
        object.__setattr__(value, "_parent_count", parent_count)
        object.__setattr__(value, "_detail", detail)
        return value

    @property
    def kind(self) -> ParentVerdictKind:
        return self._kind

    @property
    def commit_sha(self) -> str | None:
        return self._commit_sha

    @property
    def parent_count(self) -> int | None:
        return self._parent_count

    @property
    def detail(self) -> str:
        return self._detail

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "commit_sha": self.commit_sha,
            "parent_count": self.parent_count,
            "detail": self.detail,
        }


class _ReportedParentVerdict:
    __slots__ = ("verdict",)

    def __init__(self, verdict: ParentVerdict) -> None:
        self.verdict = verdict


class CompletedMerge:
    """A merge receipt whose parent verdict has been consumed.

    Direct construction is unavailable. `consume_merge_receipt` verifies and
    reports first, then mints this record from a private reported-verdict
    carrier. A caller may retain the record, but cannot obtain one merely by
    holding a raw receipt and choosing to skip the verdict.
    """

    __slots__ = ("_parent_verdict", "_receipt")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("CompletedMerge is constructed by consume_merge_receipt")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("CompletedMerge is immutable")

    @classmethod
    def _from_reported(
        cls,
        receipt: MergeReceipt,
        reported: _ReportedParentVerdict,
    ) -> CompletedMerge:
        if not isinstance(reported, _ReportedParentVerdict):
            raise TypeError("completion requires a reported parent verdict")
        value = object.__new__(cls)
        object.__setattr__(value, "_receipt", receipt)
        object.__setattr__(value, "_parent_verdict", reported.verdict)
        return value

    @property
    def receipt(self) -> MergeReceipt:
        return self._receipt

    @property
    def parent_verdict(self) -> ParentVerdict:
        return self._parent_verdict

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.receipt.observed.repo,
            "pr_number": self.receipt.observed.number,
            "url": self.receipt.observed.url,
            "commit_sha": self.receipt.commit_sha,
            "requested_method": self.receipt.requested_method.value,
            "parent_verdict": self.parent_verdict.as_dict(),
        }


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def verify_parent_shape(
    *,
    repo_root: Path,
    receipt: MergeReceipt,
    remote: str,
) -> ParentVerdict:
    """Verify the operation-returned commit in the local repository DAG."""

    commit_sha = receipt.commit_sha
    if commit_sha is None:
        return ParentVerdict._earned(
            kind=ParentVerdictKind.UNVERIFIABLE,
            commit_sha=None,
            parent_count=None,
            detail="the host returned no immutable merge commit identity",
        )

    if receipt.requested_method in {MergeMethod.SQUASH, MergeMethod.REBASE}:
        return ParentVerdict._earned(
            kind=ParentVerdictKind.NOT_APPLICABLE,
            commit_sha=commit_sha,
            parent_count=None,
            detail=f"{receipt.requested_method.value} merges are single-parent by design",
        )

    repo_root = Path(repo_root)
    try:
        fetch = _run_git(
            repo_root,
            "fetch",
            "--no-write-fetch-head",
            remote,
            commit_sha,
        )
    except OSError as exc:
        return _unverifiable(commit_sha, f"could not execute git fetch: {exc}")
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "git fetch failed"
        return _unverifiable(commit_sha, f"could not fetch receipt object: {detail}")

    try:
        opened = _run_git(repo_root, "rev-list", "--parents", "-n", "1", commit_sha)
    except OSError as exc:
        return _unverifiable(commit_sha, f"could not inspect receipt object: {exc}")
    if opened.returncode != 0:
        detail = opened.stderr.strip() or opened.stdout.strip() or "git rev-list failed"
        return _unverifiable(commit_sha, f"could not inspect receipt object: {detail}")

    rows = [line.split() for line in opened.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or not rows[0] or rows[0][0] != commit_sha:
        return _unverifiable(
            commit_sha,
            "local DAG did not return the exact receipt commit",
        )
    parent_count = len(rows[0]) - 1
    if parent_count == 2:
        return ParentVerdict._earned(
            kind=ParentVerdictKind.VERIFIED,
            commit_sha=commit_sha,
            parent_count=parent_count,
            detail=f"merge commit {commit_sha} has two parents in the local DAG",
        )
    return ParentVerdict._earned(
        kind=ParentVerdictKind.WRONG,
        commit_sha=commit_sha,
        parent_count=parent_count,
        detail=(
            f"merge commit {commit_sha} has {parent_count} parents in the local DAG; "
            "expected 2"
        ),
    )


def _unverifiable(commit_sha: str, detail: str) -> ParentVerdict:
    return ParentVerdict._earned(
        kind=ParentVerdictKind.UNVERIFIABLE,
        commit_sha=commit_sha,
        parent_count=None,
        detail=detail,
    )


def report_parent_verdict(
    verdict: ParentVerdict,
    *,
    report: Callable[[str], Any],
) -> _ReportedParentVerdict:
    if verdict.kind is ParentVerdictKind.NOT_PERFORMED:
        raise TypeError("an unperformed parent verdict cannot complete a merge")
    if verdict.kind in {ParentVerdictKind.WRONG, ParentVerdictKind.UNVERIFIABLE}:
        report(f"merge parent verification {verdict.kind.value}: {verdict.detail}")
    return _ReportedParentVerdict(verdict)


def consume_merge_receipt(
    *,
    repo_root: Path,
    receipt: MergeReceipt,
    remote: str,
    report: Callable[[str], Any],
) -> CompletedMerge:
    verdict = verify_parent_shape(
        repo_root=repo_root,
        receipt=receipt,
        remote=remote,
    )
    reported = report_parent_verdict(verdict, report=report)
    return CompletedMerge._from_reported(receipt, reported)

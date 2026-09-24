from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PRRef:
    repo: str
    number: int | None = None
    url: str | None = None
    head_branch: str | None = None
    base_branch: str | None = None
    title: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def pr_number_from_url(url: str) -> int:
    """The PR number, parsed out of the URL that ``gh pr create`` prints.

    ``gh pr create`` prints only the URL, so the number has to be read from it. This
    REFUSES rather than returning None, because ``PRRef.number`` defaults to None: a
    silent None is what put ``pr_number: null`` into a PR group's JSON while the
    ``url`` beside it carried the number, and a caller linking a set of PRs cannot
    tell "this PR has no number" from "nobody parsed it".
    """
    match = re.search(r"/pull/(\d+)/?$", (url or "").strip())
    if match is None:
        raise AdapterError(f"gh pr create returned a URL carrying no PR number: {url!r}")
    return int(match.group(1))


class MergeMethod(StrEnum):
    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"

    @property
    def gh_flag(self) -> str:
        return f"--{self.value}"


@dataclass(frozen=True)
class MergeReceipt:
    """Host-observed evidence for one completed merge operation.

    `requested` is caller intent. `observed` and `commit_sha` come from the
    hosting platform's structured PR record after the merge command returns.
    Keeping both identities prevents a response for a different PR from being
    accepted merely because the command exited successfully.

    `commit_sha=None` has one meaning: the host explicitly returned no merge
    commit identity. It never means parent verification succeeded.
    """

    requested: PRRef
    observed: PRRef
    commit_sha: str | None
    requested_method: MergeMethod

    def as_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested.as_dict(),
            "observed": self.observed.as_dict(),
            "commit_sha": self.commit_sha,
            "requested_method": self.requested_method.value,
        }


@dataclass(frozen=True)
class PRCheck:
    name: str
    status: str
    conclusion: str | None = None
    details_url: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PRStatus:
    ref: PRRef
    state: str
    mergeable: str | None = None
    checks: list[PRCheck] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.as_dict(),
            "state": self.state,
            "mergeable": self.mergeable,
            "checks": [item.as_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class CreatePRRequest:
    repo: str
    title: str
    body: str
    head_branch: str
    base_branch: str
    draft: bool = False


class PlatformAdapter(Protocol):
    """Protocol for platform-backed PR orchestration.

    gr2 owns the orchestration UX. Adapters hide the hosting platform backend.
    """

    name: str

    def create_pr(self, request: CreatePRRequest) -> PRRef: ...

    def merge_pr(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod,
    ) -> MergeReceipt: ...

    def pr_status(self, repo: str, number: int) -> PRStatus: ...

    def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]: ...

    def pr_checks(self, repo: str, number: int) -> list[PRCheck]: ...

    def edit_pr_body(self, repo: str, number: int, body: str) -> None: ...


class AdapterError(RuntimeError):
    pass


class MergeEvidenceError(AdapterError):
    """The host acknowledged a merge, but its immutable receipt is unavailable.

    This is deliberately distinct from a failed merge command. Retrying after
    this error could merge or report the same operation twice.
    """

    operation_acknowledged = True

    def __init__(
        self,
        *,
        requested: PRRef,
        requested_method: MergeMethod,
        reason: str,
    ) -> None:
        self.requested = requested
        self.requested_method = requested_method
        self.reason = reason
        super().__init__(
            f"merge command succeeded for {requested.repo}#{requested.number}, "
            f"but its immutable receipt is unavailable: {reason}"
        )


@contextlib.contextmanager
def body_file(body: str) -> Iterator[str]:
    """Write ``body`` to a temp file and yield its path, removing it on the way out.

    Every gh call that carries a body goes through a file: a PR body holds other
    PRs' URLs and arbitrary caller text, and argv is a world-readable, length-limited
    surface. Returns the path so the caller can pass ``--body-file <path>``.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        path = handle.name
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


def _run_json(command: list[str], *, cwd: Path | None = None) -> object:
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AdapterError(
            f"could not launch command for structured host evidence: {command[0]!r}: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise AdapterError(detail or f"command failed: {' '.join(command)}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"command did not return valid json: {' '.join(command)}") from exc


class GitHubAdapter:
    name = "github"

    def __init__(self, gh_binary: str = "gh") -> None:
        if shutil.which(gh_binary) is None:
            raise AdapterError(f"`{gh_binary}` not found in PATH")
        self.gh_binary = gh_binary

    def create_pr(self, request: CreatePRRequest) -> PRRef:
        cmd = [
            self.gh_binary,
            "pr",
            "create",
            "--repo",
            request.repo,
            "--title",
            request.title,
            "--head",
            request.head_branch,
            "--base",
            request.base_branch,
        ]
        if request.draft:
            cmd.append("--draft")
        with body_file(request.body) as body_path:
            proc = subprocess.run(
                [*cmd, "--body-file", body_path],
                capture_output=True,
                text=True,
                check=False,
            )
        if proc.returncode != 0:
            raise AdapterError(proc.stderr.strip() or proc.stdout.strip() or "gh pr create failed")
        url = proc.stdout.strip()
        return PRRef(
            repo=request.repo,
            number=pr_number_from_url(url),
            url=url or None,
            head_branch=request.head_branch,
            base_branch=request.base_branch,
            title=request.title,
        )

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        """Replace one PR's body, through the same file path ``create_pr`` uses."""
        with body_file(body) as body_path:
            proc = subprocess.run(
                [self.gh_binary, "pr", "edit", str(number), "--repo", repo, "--body-file", body_path],
                capture_output=True,
                text=True,
                check=False,
            )
        if proc.returncode != 0:
            raise AdapterError(
                proc.stderr.strip() or proc.stdout.strip() or f"gh pr edit failed for {repo}#{number}"
            )

    def merge_pr(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod,
    ) -> MergeReceipt:
        proc = subprocess.run(
            [
                self.gh_binary,
                "pr",
                "merge",
                str(number),
                "--repo",
                repo,
                method.gh_flag,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AdapterError(proc.stderr.strip() or proc.stdout.strip() or "gh pr merge failed")

        # `gh pr merge` is silent on successful non-interactive merges. Read
        # the immutable PR record rather than reconstructing a receipt from
        # the command inputs or asking what commit the base points at now.
        requested = PRRef(repo=repo, number=number)
        try:
            payload = _run_json(
                [
                    self.gh_binary,
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    repo,
                    "--json",
                    "number,url,state,mergeCommit",
                ]
            )
            return _parse_merge_receipt(
                payload,
                requested=requested,
                requested_method=method,
            )
        except AdapterError as exc:
            raise MergeEvidenceError(
                requested=requested,
                requested_method=method,
                reason=str(exc),
            ) from exc

    def pr_status(self, repo: str, number: int) -> PRStatus:
        payload = _run_json(
            [
                self.gh_binary,
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,url,headRefName,baseRefName,title,state,mergeable,statusCheckRollup",
            ]
        )
        assert isinstance(payload, dict)
        checks = self._parse_checks(payload.get("statusCheckRollup") or [])
        ref = PRRef(
            repo=repo,
            number=payload.get("number"),
            url=payload.get("url"),
            head_branch=payload.get("headRefName"),
            base_branch=payload.get("baseRefName"),
            title=payload.get("title"),
        )
        return PRStatus(
            ref=ref,
            state=str(payload.get("state", "UNKNOWN")),
            mergeable=(
                str(payload.get("mergeable"))
                if payload.get("mergeable") is not None
                else None
            ),
            checks=checks,
        )

    def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]:
        payload = _run_json(
            [
                self.gh_binary,
                "pr",
                "list",
                "--repo",
                repo,
                "--json",
                "number,url,headRefName,baseRefName,title",
            ]
        )
        assert isinstance(payload, list)
        refs: list[PRRef] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if head_branch and item.get("headRefName") != head_branch:
                continue
            refs.append(
                PRRef(
                    repo=repo,
                    number=item.get("number"),
                    url=item.get("url"),
                    head_branch=item.get("headRefName"),
                    base_branch=item.get("baseRefName"),
                    title=item.get("title"),
                )
            )
        return refs

    def pr_checks(self, repo: str, number: int) -> list[PRCheck]:
        return self.pr_status(repo, number).checks

    @staticmethod
    def _parse_checks(rows: list[object]) -> list[PRCheck]:
        checks: list[PRCheck] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            checks.append(
                PRCheck(
                    name=str(row.get("name", "unknown")),
                    status=str(row.get("status", "UNKNOWN")),
                    conclusion=(
                        str(row["conclusion"])
                        if row.get("conclusion") is not None
                        else None
                    ),
                    details_url=row.get("detailsUrl"),
                )
            )
        return checks


def get_platform_adapter(name: str) -> PlatformAdapter:
    normalized = name.strip().lower()
    if normalized in {"github", "gh"}:
        return GitHubAdapter()
    raise AdapterError(f"unknown platform adapter: {name}")


_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _parse_merge_receipt(
    payload: object,
    *,
    requested: PRRef,
    requested_method: MergeMethod,
) -> MergeReceipt:
    if not isinstance(payload, dict):
        raise AdapterError("gh pr view did not return an object")
    if payload.get("state") != "MERGED":
        raise AdapterError(
            "gh pr merge returned success but the structured PR record is not MERGED"
        )

    number = payload.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise AdapterError("merged PR record has no integer number")
    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise AdapterError("merged PR record has no URL")

    observed_repo, url_number = _repo_and_number_from_pr_url(url)
    observed = PRRef(repo=observed_repo, number=number, url=url)
    if number != requested.number or url_number != requested.number:
        raise AdapterError(
            f"merged PR record identifies #{number} at URL #{url_number}, "
            f"expected #{requested.number}"
        )
    if observed.repo != requested.repo:
        raise AdapterError(
            f"merged PR record identifies repo {observed.repo!r}, "
            f"expected {requested.repo!r}"
        )

    if "mergeCommit" not in payload:
        raise AdapterError("merged PR record omitted mergeCommit")
    merge_commit = payload["mergeCommit"]
    commit_sha: str | None
    if merge_commit is None:
        commit_sha = None
    elif isinstance(merge_commit, dict):
        oid = merge_commit.get("oid")
        if not isinstance(oid, str) or _GIT_OBJECT_ID.fullmatch(oid) is None:
            raise AdapterError("merged PR record contains an invalid mergeCommit.oid")
        commit_sha = oid
    else:
        raise AdapterError("merged PR record contains a malformed mergeCommit")

    return MergeReceipt(
        requested=requested,
        observed=observed,
        commit_sha=commit_sha,
        requested_method=requested_method,
    )


def _repo_and_number_from_pr_url(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise AdapterError(f"merged PR record contains an invalid PR URL: {url!r}") from exc
    path = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or bool(parsed.query)
        or bool(parsed.fragment)
        or len(path) != 4
        or path[2] != "pull"
    ):
        raise AdapterError(f"merged PR record contains an invalid PR URL: {url!r}")
    try:
        number = int(path[3])
    except ValueError as exc:
        raise AdapterError(f"merged PR record contains an invalid PR URL: {url!r}") from exc
    return f"{path[0]}/{path[1]}", number

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


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

    def merge_pr(self, repo: str, number: int) -> PRRef: ...

    def pr_status(self, repo: str, number: int) -> PRStatus: ...

    def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]: ...

    def pr_checks(self, repo: str, number: int) -> list[PRCheck]: ...


class AdapterError(RuntimeError):
    pass


def _run_json(command: list[str], *, cwd: Path | None = None) -> object:
    proc = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AdapterError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {' '.join(command)}")
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
            "--body",
            request.body,
            "--head",
            request.head_branch,
            "--base",
            request.base_branch,
        ]
        if request.draft:
            cmd.append("--draft")
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AdapterError(proc.stderr.strip() or proc.stdout.strip() or "gh pr create failed")
        url = proc.stdout.strip()
        return PRRef(
            repo=request.repo,
            url=url or None,
            head_branch=request.head_branch,
            base_branch=request.base_branch,
            title=request.title,
        )

    def merge_pr(self, repo: str, number: int) -> PRRef:
        proc = subprocess.run(
            [self.gh_binary, "pr", "merge", str(number), "--repo", repo],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AdapterError(proc.stderr.strip() or proc.stdout.strip() or "gh pr merge failed")
        return PRRef(repo=repo, number=number)

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
            mergeable=str(payload.get("mergeable")) if payload.get("mergeable") is not None else None,
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
                    conclusion=(str(row["conclusion"]) if row.get("conclusion") is not None else None),
                    details_url=row.get("detailsUrl"),
                )
            )
        return checks


def get_platform_adapter(name: str) -> PlatformAdapter:
    normalized = name.strip().lower()
    if normalized in {"github", "gh"}:
        return GitHubAdapter()
    raise AdapterError(f"unknown platform adapter: {name}")

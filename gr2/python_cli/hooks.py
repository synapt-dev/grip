from __future__ import annotations

import dataclasses
import json
import sys
import subprocess
import time
import tomllib
from pathlib import Path

from .events import emit, EventType


VALID_IF_EXISTS = {"skip", "overwrite", "merge", "error"}
VALID_ON_FAILURE = {"block", "warn", "skip"}
VALID_WHEN = {"first_materialize", "always", "dirty", "manual"}


@dataclasses.dataclass(frozen=True)
class FileProjection:
    kind: str
    src: str
    dest: str
    if_exists: str = "error"


@dataclasses.dataclass(frozen=True)
class LifecycleHook:
    stage: str
    name: str
    command: str
    cwd: str
    when: str
    on_failure: str


@dataclasses.dataclass(frozen=True)
class RepoHooks:
    repo_name: str | None
    file_links: list[FileProjection]
    file_copies: list[FileProjection]
    on_materialize: list[LifecycleHook]
    on_enter: list[LifecycleHook]
    on_exit: list[LifecycleHook]
    policy: dict[str, object]
    path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "repo_name": self.repo_name,
            "path": str(self.path),
            "files": {
                "link": [dataclasses.asdict(item) for item in self.file_links],
                "copy": [dataclasses.asdict(item) for item in self.file_copies],
            },
            "lifecycle": {
                "on_materialize": [dataclasses.asdict(item) for item in self.on_materialize],
                "on_enter": [dataclasses.asdict(item) for item in self.on_enter],
                "on_exit": [dataclasses.asdict(item) for item in self.on_exit],
            },
            "policy": self.policy,
        }


@dataclasses.dataclass(frozen=True)
class HookContext:
    workspace_root: Path
    lane_root: Path
    repo_root: Path
    repo_name: str
    lane_owner: str
    lane_subject: str
    lane_name: str


@dataclasses.dataclass(frozen=True)
class HookResult:
    kind: str
    name: str
    status: str
    detail: str
    cwd: str | None = None
    command: str | None = None
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    src: str | None = None
    dest: str | None = None

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


class HookRuntimeError(SystemExit):
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        super().__init__(json.dumps(payload, indent=2))


def hook_file(repo_root: Path) -> Path:
    return repo_root / ".gr2" / "hooks.toml"


def load_repo_hooks(repo_root: Path) -> RepoHooks | None:
    path = hook_file(repo_root)
    if not path.exists():
        return None
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return RepoHooks(
        repo_name=raw.get("repo", {}).get("name"),
        file_links=_parse_projections(raw, "link"),
        file_copies=_parse_projections(raw, "copy"),
        on_materialize=_parse_lifecycle(raw, "on_materialize", default_on_failure="block"),
        on_enter=_parse_lifecycle(raw, "on_enter", default_on_failure="warn"),
        on_exit=_parse_lifecycle(raw, "on_exit", default_on_failure="warn"),
        policy=dict(raw.get("policy", {})),
        path=path,
    )


def _parse_projections(raw: dict, kind: str) -> list[FileProjection]:
    items = raw.get("files", {}).get(kind, [])
    results: list[FileProjection] = []
    for item in items:
        if_exists = str(item.get("if_exists", "error"))
        if if_exists not in VALID_IF_EXISTS:
            raise SystemExit(f"invalid if_exists={if_exists} in {kind} projection")
        results.append(
            FileProjection(
                kind=kind,
                src=str(item["src"]),
                dest=str(item["dest"]),
                if_exists=if_exists,
            )
        )
    return results


def _parse_lifecycle(raw: dict, stage: str, default_on_failure: str) -> list[LifecycleHook]:
    items = raw.get("lifecycle", {}).get(stage, [])
    results: list[LifecycleHook] = []
    for item in items:
        when = str(item.get("when", "always"))
        on_failure = str(item.get("on_failure", default_on_failure))
        if when not in VALID_WHEN:
            raise SystemExit(f"invalid when={when} in lifecycle.{stage}")
        if on_failure not in VALID_ON_FAILURE:
            raise SystemExit(f"invalid on_failure={on_failure} in lifecycle.{stage}")
        results.append(
            LifecycleHook(
                stage=stage,
                name=str(item["name"]),
                command=str(item["command"]),
                cwd=str(item.get("cwd", "{repo_root}")),
                when=when,
                on_failure=on_failure,
            )
        )
    return results


def render_path(template: str, ctx: HookContext) -> Path:
    rendered = render_text(template, ctx)
    return Path(rendered)


def render_text(template: str, ctx: HookContext) -> str:
    return (
        template.replace("{workspace_root}", str(ctx.workspace_root))
        .replace("{lane_root}", str(ctx.lane_root))
        .replace("{repo_root}", str(ctx.repo_root))
        .replace("{repo_name}", ctx.repo_name)
        .replace("{lane_owner}", ctx.lane_owner)
        .replace("{lane_subject}", ctx.lane_subject)
        .replace("{lane_name}", ctx.lane_name)
    )


def apply_file_projections(hooks: RepoHooks, ctx: HookContext) -> list[HookResult]:
    results: list[HookResult] = []
    for item in [*hooks.file_links, *hooks.file_copies]:
        rendered_src = render_text(item.src, ctx)
        src = Path(rendered_src)
        if not src.is_absolute():
            src = ctx.repo_root / src
        dest = render_path(item.dest, ctx)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if not src.exists():
            raise HookRuntimeError(
                {
                    "kind": "projection",
                    "projection": item.kind,
                    "status": "blocked",
                    "detail": f"projection source does not exist: {src}",
                    "repo_hooks_path": str(hooks.path),
                    "src": str(src),
                    "dest": str(dest),
                }
            )

        if dest.exists() or dest.is_symlink():
            if item.if_exists == "skip":
                results.append(
                    HookResult(
                        kind="projection",
                        name=f"{item.kind}:{dest.name}",
                        status="skipped",
                        detail=f"destination already exists and if_exists=skip: {dest}",
                        src=str(src),
                        dest=str(dest),
                    )
                )
                continue
            if item.if_exists == "error":
                raise HookRuntimeError(
                    {
                        "kind": "projection",
                        "projection": item.kind,
                        "status": "blocked",
                        "detail": f"projection conflict at {dest}",
                        "repo_hooks_path": str(hooks.path),
                        "src": str(src),
                        "dest": str(dest),
                    }
                )
            if item.if_exists == "merge":
                raise HookRuntimeError(
                    {
                        "kind": "projection",
                        "projection": item.kind,
                        "status": "blocked",
                        "detail": f"merge projections not implemented yet for {dest}",
                        "repo_hooks_path": str(hooks.path),
                        "src": str(src),
                        "dest": str(dest),
                    }
                )
            if item.if_exists == "overwrite":
                if dest.is_dir() and not dest.is_symlink():
                    raise HookRuntimeError(
                        {
                            "kind": "projection",
                            "projection": item.kind,
                            "status": "blocked",
                            "detail": f"refusing to overwrite directory projection target: {dest}",
                            "repo_hooks_path": str(hooks.path),
                            "src": str(src),
                            "dest": str(dest),
                        }
                    )
                dest.unlink(missing_ok=True)

        if item.kind == "link":
            dest.symlink_to(src)
        else:
            dest.write_bytes(src.read_bytes())
        results.append(
            HookResult(
                kind="projection",
                name=f"{item.kind}:{dest.name}",
                status="applied",
                detail=f"{item.kind} {src} -> {dest}",
                src=str(src),
                dest=str(dest),
            )
        )
    return results


def run_lifecycle_stage(
    hooks: RepoHooks,
    stage: str,
    ctx: HookContext,
    *,
    repo_dirty: bool,
    first_materialize: bool,
    allow_manual: bool = False,
) -> list[HookResult]:
    hooks_for_stage = {
        "on_materialize": hooks.on_materialize,
        "on_enter": hooks.on_enter,
        "on_exit": hooks.on_exit,
    }[stage]
    results: list[HookResult] = []
    for hook in hooks_for_stage:
        if not _should_run(
            hook.when,
            repo_dirty=repo_dirty,
            first_materialize=first_materialize,
            allow_manual=allow_manual,
        ):
            emit(
                event_type=EventType.HOOK_SKIPPED,
                workspace_root=ctx.workspace_root,
                actor="system",
                owner_unit=ctx.lane_owner,
                payload={
                    "stage": stage,
                    "hook_name": hook.name,
                    "repo": ctx.repo_name,
                    "reason": f"when={hook.when} did not match current invocation",
                },
            )
            results.append(
                HookResult(
                    kind="lifecycle",
                    name=hook.name,
                    status="skipped",
                    detail=f"hook when={hook.when} did not match current invocation",
                )
            )
            continue
        cwd = render_path(hook.cwd, ctx)
        command = render_text(hook.command, ctx)
        emit(
            event_type=EventType.HOOK_STARTED,
            workspace_root=ctx.workspace_root,
            actor="system",
            owner_unit=ctx.lane_owner,
            payload={
                "stage": stage,
                "hook_name": hook.name,
                "repo": ctx.repo_name,
                "command": command,
                "cwd": str(cwd),
            },
        )
        t0 = time.monotonic()
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        if proc.returncode == 0:
            emit(
                event_type=EventType.HOOK_COMPLETED,
                workspace_root=ctx.workspace_root,
                actor="system",
                owner_unit=ctx.lane_owner,
                payload={
                    "stage": stage,
                    "hook_name": hook.name,
                    "repo": ctx.repo_name,
                    "duration_ms": duration_ms,
                    "exit_code": 0,
                },
            )
            results.append(
                HookResult(
                    kind="lifecycle",
                    name=hook.name,
                    status="applied",
                    detail=f"stage {stage} completed successfully",
                    cwd=str(cwd),
                    command=command,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            )
            continue
        stderr_tail = proc.stderr[-500:] if proc.stderr else ""
        emit(
            event_type=EventType.HOOK_FAILED,
            workspace_root=ctx.workspace_root,
            actor="system",
            owner_unit=ctx.lane_owner,
            payload={
                "stage": stage,
                "hook_name": hook.name,
                "repo": ctx.repo_name,
                "duration_ms": duration_ms,
                "exit_code": proc.returncode,
                "on_failure": hook.on_failure,
                "stderr_tail": stderr_tail,
            },
        )
        payload = {
            "kind": "lifecycle",
            "stage": stage,
            "hook": hook.name,
            "cwd": str(cwd),
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "on_failure": hook.on_failure,
        }
        if hook.on_failure == "block":
            raise HookRuntimeError(payload)
        if hook.on_failure == "warn":
            print(json.dumps(payload, indent=2), file=sys.stderr)
            results.append(
                HookResult(
                    kind="lifecycle",
                    name=hook.name,
                    status="warned",
                    detail=f"hook failed with on_failure=warn during {stage}",
                    cwd=str(cwd),
                    command=command,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            )
            continue
        results.append(
            HookResult(
                kind="lifecycle",
                name=hook.name,
                status="skipped",
                detail=f"hook failed with on_failure=skip during {stage}",
                cwd=str(cwd),
                command=command,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        )
    return results


def _should_run(when: str, *, repo_dirty: bool, first_materialize: bool, allow_manual: bool) -> bool:
    if when == "always":
        return True
    if when == "first_materialize":
        return first_materialize
    if when == "dirty":
        return repo_dirty
    if when == "manual":
        return allow_manual
    return False

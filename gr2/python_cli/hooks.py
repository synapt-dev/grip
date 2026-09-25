from __future__ import annotations

import dataclasses
import json
import sys
import subprocess
import time
import tomllib
from pathlib import Path

from .events import EventType, emit, emit_after_outcome


VALID_IF_EXISTS = {"skip", "overwrite", "merge", "error"}
VALID_ON_FAILURE = {"block", "warn", "skip"}
VALID_WHEN = {"first_materialize", "always", "dirty", "manual"}
VALID_STAGES = {"on_materialize", "on_enter", "on_exit"}


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
    # the consent gate: consent-shaped sections found in the hooks table.
    # Consent is a LOCAL record on the host (consent.py); a section inside
    # the table arrives pre-consented from whoever authored it and must
    # never bind — it is collected here, ignored, and REPORTED.
    ignored_consent_keys: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "repo_name": self.repo_name,
            "path": str(self.path),
            "ignored_consent_keys": list(self.ignored_consent_keys),
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
    unit_root: Path
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
    if_exists: str | None = None

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
    # The wrong-shape guard: a hooks table whose stage keys are TOP-LEVEL
    # ([[on_enter]] instead of [[lifecycle.on_enter]]) builds an empty RepoHooks,
    # and every verb downstream succeeds while the author's hook intent has
    # vanished without a trace (measured). Refuse at load, naming
    # the expected shape. Consent is a LOCAL record on the host, never a grant
    # inside the table: a consent-shaped section is collected and ignored.
    stray = [key for key in raw if str(key).lower() in VALID_STAGES]
    if stray:
        raise SystemExit(
            f"invalid hooks table {path}: lifecycle stage(s) {', '.join(sorted(stray))} "
            f"are top-level; stages must live under [[lifecycle.on_materialize]] / "
            "[[lifecycle.on_enter]] / [[lifecycle.on_exit]]"
        )
    ignored = [key for key in raw if "consent" in str(key).lower()]
    nested_hooks = raw.get("hooks")
    if isinstance(nested_hooks, dict):
        ignored.extend(
            "hooks." + key for key in nested_hooks if "consent" in str(key).lower()
        )
    return RepoHooks(
        repo_name=raw.get("repo", {}).get("name"),
        file_links=_parse_projections(raw, "link"),
        file_copies=_parse_projections(raw, "copy"),
        on_materialize=_parse_lifecycle(raw, "on_materialize", default_on_failure="block"),
        on_enter=_parse_lifecycle(raw, "on_enter", default_on_failure="warn"),
        on_exit=_parse_lifecycle(raw, "on_exit", default_on_failure="warn"),
        policy=dict(raw.get("policy", {})),
        path=path,
        ignored_consent_keys=ignored,
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
    import re

    result = (
        template.replace("{workspace_root}", str(ctx.workspace_root))
        .replace("{unit_root}", str(ctx.unit_root))
        .replace("{lane_root}", str(ctx.lane_root))
        .replace("{repo_root}", str(ctx.repo_root))
        .replace("{repo_name}", ctx.repo_name)
        .replace("{lane_owner}", ctx.lane_owner)
        .replace("{lane_subject}", ctx.lane_subject)
        .replace("{lane_name}", ctx.lane_name)
    )
    remaining = re.findall(r"\{(\w+)\}", result)
    if remaining:
        raise ValueError(
            f"undefined template variable(s): {', '.join('{' + v + '}' for v in remaining)}"
        )
    return result


def _consent_violations(
    dest_template: str, ctx: HookContext, *, rendered: str, kind: str | None = None, link_target: str | None = None
) -> list[str]:
    """The confinement check for one projection dest (the consent gate scope
    measured 2026-09-24): the boundary is the workspace
    root (the root and {unit_root} are inside), never under any .git,
    never inside another member's tree, and a link's TARGET must resolve
    inside the member's own tree. Nothing is invented."""
    from . import consent as _consent

    return _consent.confinement_violations(
        dest_template,
        ctx.repo_root,
        ctx.workspace_root,
        rendered=rendered,
        sibling_member_roots=_consent.declared_member_roots(ctx.workspace_root, exclude=ctx.repo_root),
        kind=kind,
        link_target=link_target,
    )


# the kwarg default for gate=: "not supplied". A module sentinel, because
# gate=None already means "the gate ran and found a bound member with no
# pending marker" — an overloaded None would make both callees call the
# gate again for that most common state.
_GATE_UNSET = object()


def apply_file_projections(hooks: RepoHooks, ctx: HookContext, *, gate: tuple[str, str, dict | None, bool] | object = _GATE_UNSET) -> list[HookResult]:
    if gate is _GATE_UNSET:
        gate = _consent_gate(ctx)
    if gate is not None and gate[1] != "bound":
        # the consent gate: projections are gated by the SAME consent record
        # (the measurement showed they escape the member tree and land under
        # .git, inside gr2's state area, and at arbitrary absolute paths).
        # SKIP AND REPORT, like the lifecycle gate; nothing is written. A
        # bound-with-pending gate falls through and runs.
        key, state, record = gate[0], gate[1], gate[2]
        items = [*hooks.file_links, *hooks.file_copies]
        results: list[HookResult] = [
            HookResult(
                kind="projection",
                name=f"{item.kind}:{item.dest}",
                status=state,
                detail=f"member hooks {state}; projection not written: {item.dest}",
                dest=item.dest,
                if_exists=item.if_exists,
            )
            for item in items
        ]
        _report_unbound(
            ctx,
            key,
            state,
            record,
            [(f"projection {item.kind}", item.dest) for item in items],
            list(hooks.ignored_consent_keys),
        )
        return results
    results: list[HookResult] = []
    for item in [*hooks.file_links, *hooks.file_copies]:
        rendered_src = render_text(item.src, ctx)
        src = Path(rendered_src)
        if not src.is_absolute():
            src = ctx.repo_root / src
        # the projection SOURCE resolves inside the
        # member's own tree, like a link target — a symlinked src pointing
        # outside must not smuggle outside bytes into the workspace.
        if not src.resolve().is_relative_to(ctx.repo_root.resolve()):
            raise HookRuntimeError(
                {
                    "kind": "projection",
                    "projection": item.kind,
                    "status": "refused",
                    "detail": "confinement: projection source resolves outside the member's own tree",
                    "repo_hooks_path": str(hooks.path),
                    "src": str(src),
                    "dest": item.dest,
                    "if_exists": item.if_exists,
                }
            )
        dest = render_path(item.dest, ctx)
        # consent answers whether this repo may act; confinement answers
        # where. Every dest RESOLVES (symlinks included) inside the
        # workspace root, never under any .git, never inside another
        # member's tree; for a link, the TARGET must resolve inside the
        # member's own tree. A violating row is REFUSED and REPORTED even
        # when consent is GRANTED — checked BEFORE the dest parent is
        # created, so a violation writes nothing.
        violations = _consent_violations(
            item.dest,
            ctx,
            rendered=str(dest),
            kind=item.kind,
            link_target=str(src.resolve()) if item.kind == "link" else None,
        )
        if violations:
            raise HookRuntimeError(
                {
                    "kind": "projection",
                    "projection": item.kind,
                    "status": "refused",
                    "detail": "confinement: " + "; ".join(violations),
                    "repo_hooks_path": str(hooks.path),
                    "src": str(src),
                    "dest": str(dest),
                    "if_exists": item.if_exists,
                }
            )
        dest.parent.mkdir(parents=True, exist_ok=True)

        # A projection writes untracked space only. A tracked destination is
        # the repo's own content — silently replacing it (any if_exists) type-
        # dirties the clone and breaks every tree-exactness assertion (freezes,
        # reviews, status gates). A repo whose hooks.toml projects over its own
        # tracked file hits this on every materialization. The skip is a named
        # result so the receipt says why nothing was written.
        if dest.is_relative_to(ctx.repo_root):
            tracked = subprocess.run(
                ["git", "-C", str(ctx.repo_root), "ls-files",
                 "--error-unmatch", str(dest.relative_to(ctx.repo_root))],
                capture_output=True,
            )
            if tracked.returncode == 0:
                results.append(
                    HookResult(
                        kind="projection",
                        name=f"{item.kind}:{dest.name}",
                        status="skipped",
                        detail=(
                            "destination is tracked by the repo; "
                            "projections never modify tracked paths"
                        ),
                        src=str(src),
                        dest=str(dest),
                        if_exists=item.if_exists,
                    )
                )
                continue

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
                        if_exists=item.if_exists,
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
                if_exists=item.if_exists,
            )
        )
    return results


def run_materialize_hook_block(
    hooks: RepoHooks,
    ctx: HookContext,
    *,
    repo_dirty: bool,
    first_materialize: bool,
    allow_manual: bool = False,
) -> list[HookResult]:
    """The member's materialize hook block: projections, then on_materialize.
    Shared by both doors (workspace materialize and lane create/enter).
    The consent gate runs ONCE here and its result is passed to both halves,
    so the pending first-materialize marker is consumed once and ORs the
    first-materialize semantics for the run AND for the withheld filter.
    A refused or blocked projection row aborts the block fail-closed —
    nothing is written — and the refusal names the member's on_materialize
    hooks that were withheld (filtered by the same when rules the run
    itself applies), so a consented hook is never silently skipped. When a
    pending marker was consumed before the row raised, the marker is put
    back, so fixing the row and re-trusting still runs the deferred hook."""
    from . import consent as _consent

    gate = _consent_gate(ctx)
    gate_key, gate_state, _record, gate_pending = gate if gate is not None else (None, None, None, False)
    try:
        projections = apply_file_projections(hooks, ctx, gate=gate)
    except HookRuntimeError as exc:
        payload = dict(exc.payload)
        if gate_pending:
            # the single gate call consumed the pending marker before the
            # row raised: put it back so the deferred hook survives the
            # refusal and runs once the row is fixed and re-trusted
            _consent.write_pending_marker(ctx.workspace_root, gate_key)
        due = [
            f"{hook.name}: {hook.command}"
            for hook in hooks.on_materialize
            if _should_run(
                hook.when,
                repo_dirty=repo_dirty,
                first_materialize=first_materialize or gate_pending,
                allow_manual=allow_manual,
            )
        ]
        payload["lifecycle_hooks_withheld"] = due
        if str(payload.get("status")) == "refused":
            payload["lifecycle_hooks_withheld_detail"] = (
                "the member's on_materialize hooks did not run: a projection row in"
                " this member's hooks table was refused; fix or remove the row and"
                " re-trust (the record lapses by hash)"
            )
        else:
            payload["lifecycle_hooks_withheld_detail"] = (
                "the member's on_materialize hooks did not run: "
                + str(payload.get("detail", "the hook block was aborted"))
            )
        raise HookRuntimeError(payload) from exc
    run_lifecycle_stage(
        hooks,
        "on_materialize",
        ctx,
        repo_dirty=repo_dirty,
        first_materialize=first_materialize,
        allow_manual=allow_manual,
        gate=gate,
    )
    return projections


def _consent_gate(
    ctx: HookContext,
) -> tuple[str, str, dict | None, bool] | None:
    """the consent gate: the consent check shared by both hook classes.

    Returns None when the member's hooks are BOUND and no pending
    first-materialize marker exists. The bool is True when a pending marker
    was consumed (hooks skipped on the unbound first materialize
    run once on the next bound materialize). Otherwise returns
    (member_key, state, record, False) — the caller skips and reports, and
    the skip writes a pending marker. The member key resolves through the
    workspace spec so a lane's materialized copy of a member maps back to
    the member's declared path: consent is granted once per member, never
    per lane copy.
    """
    from . import consent as _consent

    key = _consent.member_key(ctx.workspace_root, ctx.repo_root, ctx.repo_name)
    state, record = _consent.consent_state(ctx.workspace_root, key, ctx.repo_root)
    if state == "bound":
        if not _consent.take_pending_marker(ctx.workspace_root, key):
            return None
        return (key, "bound", record, True)
    _consent.write_pending_marker(ctx.workspace_root, key)
    return (key, state, record, False)


def _report_unbound(
    ctx: HookContext, key: str, state: str, record: dict | None, items: list[tuple[str, str]], ignored: list[str]
) -> None:
    """SKIP AND REPORT: the verb completes with exit
    0 and prints what did NOT run plus the bind command. Refusing was ruled
    out — it breaks the stranger's README walk."""
    from . import consent as _consent

    sha = _consent.hooks_sha(ctx.repo_root)
    if state == "changed":
        old = str((record or {}).get("hooks_sha", "?"))[:12]
        print(
            f"gr2: member '{key}' hooks changed since grant "
            f"(granted sha {old}, now {sha[:12]}) — the following did NOT run:"
        )
    else:
        print(f"gr2: member '{key}' hooks unbound (sha {sha[:12]}) — the following did NOT run:")
    for name, what in items:
        print(f"  {name}: {what}")
    if ignored:
        print(
            "  ignored consent-shaped section(s) in the hooks table: "
            + ", ".join(ignored)
            + " — consent is a local record on the host, never inside the hooks table"
        )
    print(f"bind: gr2 hooks trust {ctx.repo_name}")


def run_lifecycle_stage(
    hooks: RepoHooks,
    stage: str,
    ctx: HookContext,
    *,
    repo_dirty: bool,
    first_materialize: bool,
    allow_manual: bool = False,
    gate: tuple[str, str, dict | None, bool] | object = _GATE_UNSET,
) -> list[HookResult]:
    hooks_for_stage = {
        "on_materialize": hooks.on_materialize,
        "on_enter": hooks.on_enter,
        "on_exit": hooks.on_exit,
    }[stage]
    # the consent gate: consent is the OUTER gate. An unbound or changed
    # member's commands do not run; the verb completes and reports. The
    # gate is folded into the loop below so there is ONE HOOK_SKIPPED emit
    # site (the outcome-policy counter counts them per call site) and the
    # --manual-hooks axis stays subordinate: consent blocks first, and
    # allow_manual never resurrects a consent-skipped hook.
    if gate is _GATE_UNSET:
        gate = _consent_gate(ctx)
    gate_key, gate_state, gate_record, gate_pending = gate if gate is not None else (None, None, None, False)
    if gate_state == "bound":
        # bound: proceed. A consumed pending first-materialize marker ORs the
        # first-materialize semantics — the hooks skipped on the
        # unbound first materialize run once on this bound run.
        first_materialize = first_materialize or gate_pending
        gate_key = None
    results: list[HookResult] = []
    for hook in hooks_for_stage:
        if gate_key is not None:
            skip_reason = f"member hooks {gate_state} (consent record missing or hash changed)"
            skip_status = gate_state
            skip_detail = f"member hooks {gate_state}; command not run: {hook.command}"
        elif not _should_run(
            hook.when,
            repo_dirty=repo_dirty,
            first_materialize=first_materialize,
            allow_manual=allow_manual,
        ):
            skip_reason = f"when={hook.when} did not match current invocation"
            skip_status = "skipped"
            skip_detail = f"hook when={hook.when} did not match current invocation"
        else:
            skip_reason = None
        if skip_reason is not None:
            # The ONE HOOK_SKIPPED emit site for this stage: consent-blocked
            # hooks and when-mismatched hooks both land here.
            emit(
                event_type=EventType.HOOK_SKIPPED,
                workspace_root=ctx.workspace_root,
                actor="system",
                owner_unit=ctx.lane_owner,
                payload={
                    "stage": stage,
                    "hook_name": hook.name,
                    "repo": ctx.repo_name,
                    "reason": skip_reason,
                },
            )
            results.append(
                HookResult(
                    kind="lifecycle",
                    name=hook.name,
                    status=skip_status,
                    detail=skip_detail,
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
            emit_after_outcome(
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
        emit_after_outcome(
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
    if gate_key is not None:
        # SKIP AND REPORT: the verb completes with exit 0 and
        # prints what did NOT run plus the bind command.
        _report_unbound(
            ctx,
            gate_key,
            gate_state,
            gate_record,
            [(h.name, h.command if h.command is not None else h.detail) for h in results],
            list(hooks.ignored_consent_keys),
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

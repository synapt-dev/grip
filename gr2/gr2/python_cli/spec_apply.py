from __future__ import annotations

import copy
import dataclasses
import hashlib
import hmac
import importlib.resources
import json
import os
import secrets
import stat
import sys
import tomllib
import unicodedata
import weakref
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from jsonschema import Draft202012Validator

from .events import EventType, emit_after_outcome
from . import gitops
from .gitops import (
    ensure_repo_cache,
    is_git_dir,
    is_repo_root,
    repo_dirty,
)
from .consent import consent_state, member_key as consent_member_key, pending_members
from .hooks import (
    HookContext,
    apply_file_projections,
    load_repo_hooks,
    run_lifecycle_stage,
    run_materialize_hook_block,
)


@dataclasses.dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class PlanOperation:
    kind: str
    subject: str
    target_path: str
    reason: str
    details: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def workspace_spec_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "workspace_spec.toml"


def validate_grip_toml(workspace_root: Path) -> list[ValidationIssue]:
    """Validate the Show HN v1 root spec when it is present."""
    path = workspace_root / "grip.toml"
    if not path.exists():
        return []
    with path.open("rb") as fh:
        document = tomllib.load(fh)
    schema = json.loads(importlib.resources.files("gr2.schemas").joinpath("gr2-workspace-spec-v1.schema.json").read_text())
    return [ValidationIssue("error", "grip_toml_schema", error.message, ".".join(map(str, error.path)) or None)
            for error in Draft202012Validator(schema).iter_errors(document)]


def workspace_cache_root(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "cache" / "repos"


def repo_cache_path(workspace_root: Path, repo_name: str) -> Path:
    return workspace_cache_root(workspace_root) / f"{repo_name}.git"


def load_workspace_spec_doc(workspace_root: Path) -> dict[str, object]:
    spec_path = workspace_spec_path(workspace_root)
    if not spec_path.exists():
        raise SystemExit(
            f"workspace spec not found: {spec_path}\n"
            "run `gr2 workspace init <path>` first or create .grip/workspace_spec.toml explicitly"
        )
    with spec_path.open("rb") as fh:
        return tomllib.load(fh)


def show_spec(workspace_root: Path, *, json_output: bool) -> str:
    spec_path = workspace_spec_path(workspace_root)
    if json_output:
        return json.dumps(load_workspace_spec_doc(workspace_root), indent=2)
    return spec_path.read_text()


def validate_workspace(workspace_root: Path) -> list[ValidationIssue]:
    """Validate EVERY document this root carries, not one or the other.

    A root can hold BOTH `grip.toml` (the Show HN v1 root spec) and
    `.grip/workspace_spec.toml` (the workspace spec). Until this existed,
    `spec validate` read grip.toml OR the spec -- so a root carrying both had
    exactly one of them checked, and WHICH one depended on a file being present
    rather than on the question being asked. A defect written into the unread
    document validated clean, and the caller had no way to know which half had
    been examined.

    A root carrying NEITHER is not a pass: `load_workspace_spec_doc` raises its
    own named "workspace spec not found", which is the behaviour the one-or-the-
    other path had by accident and this keeps on purpose.
    """
    has_root_spec = (workspace_root / "grip.toml").exists()
    has_workspace_spec = workspace_spec_path(workspace_root).exists()
    if not has_root_spec and not has_workspace_spec:
        load_workspace_spec_doc(workspace_root)  # raises SystemExit, naming the path
    issues: list[ValidationIssue] = []
    if has_root_spec:
        issues.extend(validate_grip_toml(workspace_root))
    if has_workspace_spec:
        issues.extend(validate_spec(workspace_root))
    return issues


def validate_spec(workspace_root: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    spec = load_workspace_spec_doc(workspace_root)

    workspace_name = str(spec.get("workspace_name", "")).strip()
    if not workspace_name:
        issues.append(
            ValidationIssue(
                level="error",
                code="missing_workspace_name",
                message="workspace spec workspace_name must not be empty",
                path="workspace_name",
            )
        )

    repo_names: set[str] = set()
    for idx, repo in enumerate(spec.get("repos", [])):
        name = str(repo.get("name", "")).strip()
        path = str(repo.get("path", "")).strip()
        url = str(repo.get("url", "")).strip()
        if not name:
            issues.append(
                ValidationIssue("error", "missing_repo_name", "repo name must not be empty", f"repos[{idx}].name")
            )
            continue
        if name in repo_names:
            issues.append(
                ValidationIssue("error", "duplicate_repo_name", f"duplicate repo '{name}'", f"repos[{idx}].name")
            )
        repo_names.add(name)
        if not path:
            issues.append(
                ValidationIssue("error", "missing_repo_path", f"repo '{name}' path must not be empty", f"repos[{idx}].path")
            )
        if not url:
            issues.append(
                ValidationIssue("error", "missing_repo_url", f"repo '{name}' url must not be empty", f"repos[{idx}].url")
            )
        # A DECLARED PATH MUST BE CONTAINED AT THIS COORDINATE TOO, not only on a
        # unit. `Path("/ws") / "/tmp/x"` IS "/tmp/x" -- pathlib DROPS the left
        # operand when the right one is absolute -- so an absolute `repos[].path`
        # silently named a directory OUTSIDE the root, and every probe below
        # (`exists`, `repo_path_state`, the hook read) then ran against that
        # outside directory. `canonicalize_workspace_path` is this module's ONE
        # containment predicate -- it also refuses `~`, backslashes, empty/`.`/`..`
        # segments, and symlinked prefixes -- and the unit coordinate already
        # delegates to it, so the two coordinates cannot drift apart.
        #
        # Reported and then FELL THROUGH as None, which is the shape the unit
        # coordinate uses two hundred lines up, so one pass names every problem in
        # the document instead of stopping at the first.
        repo_root: Path | None = None
        if path:
            try:
                repo_root = canonicalize_workspace_path(
                    workspace_root, path, field_name=f"repo '{name}' path"
                )
            except MaterializationPlanError as exc:
                issues.append(
                    ValidationIssue(
                        "error", "repo_path_outside_root", str(exc), f"repos[{idx}].path"
                    )
                )
        # `is_repo_root`, not `is_git_repo`: the latter answers
        # --is-inside-work-tree, which is true for any directory inside a
        # checkout, and a workspace root IS one -- so a plain directory at a
        # declared repo path was read as a repo and this conflict never fired.
        #
        # The state helper answers the same question the inline exemption used
        # to: a present path holds exactly one of repo_root,
        # empty_placeholder, or neither. An EMPTY directory is not a conflict
        # (the ordinary state of a freshly cloned superproject, where `git
        # clone` creates the submodule mount points and leaves them empty
        # until `submodule update --init` -- without the exemption `spec
        # validate` refused and `materialize` aborted on the first run of a
        # freshly cloned superproject, the exact path the from-superproject
        # entry exists to serve). The helper's one definition is what every
        # call site that uses the helper reads, so the three answers cannot drift apart.
        if repo_root is not None and repo_root.exists() and gitops.repo_path_state(repo_root) == "neither":
            issues.append(
                ValidationIssue(
                    level="error",
                    code="repo_path_conflict",
                    message=f"repo path exists but is not a git repo: {repo_root}",
                        path=f"repos[{idx}].path",
                    )
                )
        cache_root = repo_cache_path(workspace_root, name)
        if cache_root.exists() and not is_git_dir(cache_root):
            issues.append(
                ValidationIssue(
                    level="error",
                    code="repo_cache_conflict",
                    message=f"repo cache path exists but is not a bare git dir: {cache_root}",
                    path=f"repos[{idx}].name",
                )
            )
        # Same distinction: hooks are read from a repo root, never from a
        # directory that merely sits inside one.
        if repo_root is not None and repo_root.exists() and is_repo_root(repo_root):
            try:
                load_repo_hooks(repo_root)
            except SystemExit as exc:
                issues.append(
                    ValidationIssue(
                        level="error",
                        code="invalid_repo_hooks",
                        message=f"repo '{name}' has invalid .gr2/hooks.toml: {exc}",
                        path=f"repos[{idx}]",
                    )
                )

    unit_names: set[str] = set()
    # TWO UNITS AT ONE PATH IS SILENT OTHERWISE, and the validator here checked
    # only the NAME. Reached from the migration side: gr1 sanitises `/` to `-`,
    # so worktrees `x/y` and `x-y` both emit `../x-y` — gr1 collides identically,
    # so the translation is faithful, and this is the last place it can be
    # surfaced. One unit's worktree landing on another's with nothing reported is
    # a silent wrong ACTION, not a wrong answer.
    resolved_paths: dict[Path, str] = {}
    for idx, unit in enumerate(spec.get("units", [])):
        name = str(unit.get("name", "")).strip()
        path = str(unit.get("path", "")).strip()
        repos = [str(item) for item in unit.get("repos", [])]
        if not name:
            issues.append(
                ValidationIssue("error", "missing_unit_name", "unit name must not be empty", f"units[{idx}].name")
            )
            continue
        if name in unit_names:
            issues.append(
                ValidationIssue("error", "duplicate_unit_name", f"duplicate unit '{name}'", f"units[{idx}].name")
            )
        unit_names.add(name)
        if not path:
            issues.append(
                ValidationIssue("error", "missing_unit_path", f"unit '{name}' path must not be empty", f"units[{idx}].path")
            )
        # The unit path was never contained: `workspace_root / path` accepted an
        # absolute path, `../../x`, and a path through a symlink, and the apply
        # path then mkdir'd and wrote `unit.toml` through it. validate is where a
        # spec from a stranger is read, so the grammar is enforced HERE too.
        # Reported, then fallen through, so the repo checks below still run and
        # one pass names every problem instead of only the first.
        try:
            resolved_unit_root: Path | None = unit_root(workspace_root, unit)
        except MaterializationPlanError as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "unit_path_outside_root",
                    str(exc),
                    f"units[{idx}].path",
                )
            )
            resolved_unit_root = None

        if resolved_unit_root is not None:
            prior_unit = resolved_paths.get(resolved_unit_root)
            if prior_unit is not None:
                issues.append(
                    ValidationIssue(
                        "error",
                        "duplicate_unit_path",
                        (
                            f"units '{prior_unit}' and '{name}' both resolve to "
                            f"{resolved_unit_root} — two units at one path means one unit's "
                            "worktree lands on the other's. Distinct unit names are not "
                            "enough: gr1 sanitises `/` to `-`, so worktrees 'x/y' and 'x-y' "
                            "both map to the same sibling."
                        ),
                        f"units[{idx}].path",
                    )
                )
            else:
                resolved_paths[resolved_unit_root] = name

        # THE REFUSAL IS LIFTED, and the two lines above it in the git history are
        # the reason it existed. `migrate-gr1` legitimately declares a sibling desk
        # or the root itself, but apply was not updated for that shape: member
        # presence was checked by NAME, so it cloned `grip` into `<desk>/grip`
        # beside the desk's own `./gitgrip`, and it wrote `unit.toml` INTO the
        # desk. Items 3 and 5 are what make the shape safe, and both landed with
        # this change: members are placed by the member's SPEC PATH through ONE
        # resolver (`unit_member_path`), and a sibling unit's metadata goes under
        # `<root>/.grip/state/units/<unit>/` rather than into the desk
        # (`unit_metadata_path`). Adoption instead of re-clone was already true of
        # the resolver's contract and is now true of this path too.
        #
        # A refusal here would be a defect rather than a caution: it made
        # `migrate-gr1`'s OWN OUTPUT unappliable, which is the whole path
        # section 6b promises ("`workspace migrate-gr1` then `store init` then
        # `store commit` is the whole path").
        if (
            resolved_unit_root is not None
            and resolved_unit_root.exists()
            and resolved_unit_root.is_file()
        ):
            issues.append(
                ValidationIssue(
                    "error",
                    "unit_path_conflict",
                    f"unit path exists as a file: {resolved_unit_root}",
                    f"units[{idx}].path",
                )
            )
        # THE MEMBER COORDINATE IS CONTAINED HERE, and it is contained NOW because
        # the join that reads it moved. Members used to be placed by NAME
        # (`unit_home / member`), and a name cannot walk out of a directory, so
        # nothing had to check one. They are placed by the member's DECLARED PATH
        # now, and that value reaches a clone DESTINATION
        # (`converge_unit_repos` -> `unit_member_path` -> `clone_and_pin`), so a
        # declared path that escapes the unit home writes INTO whatever it names.
        # Measured before this check existed: a unit at "." with a member path of
        # "../desk-a/sub" passed this validator with NO issues, and planned a
        # clone whose destination is inside a sibling agent's checkout -- the one
        # property this whole path exists to protect. `../desk-a` alone resolved
        # the member to the desk ITSELF, so the desk became the member for every
        # read verb.
        #
        # THE GRAMMAR IS ALREADY THE SPEC'S, not a new one: the compiler refuses
        # `..` when it BUILDS a spec (`migration._safe_workspace_relative_path`),
        # so no spec `migrate-gr1` writes can reach this. This is the read-time
        # half of one rule, for a spec written by a stranger, and it is the same
        # helper the unit path uses -- one grammar, two coordinates.
        if resolved_unit_root is not None:
            for repo_name in repos:
                declared = _member_spec_path(spec, repo_name)
                if declared is None:
                    continue
                try:
                    canonicalize_workspace_path(
                        resolved_unit_root,
                        declared,
                        field_name=f"unit '{name}' member '{repo_name}' path",
                    )
                except MaterializationPlanError as exc:
                    issues.append(
                        ValidationIssue(
                            "error",
                            "member_path_outside_unit",
                            str(exc),
                            f"units[{idx}].repos",
                        )
                    )
        missing = [repo for repo in repos if repo not in repo_names]
        for repo_name in missing:
            issues.append(
                ValidationIssue(
                    "error",
                    "missing_unit_repo",
                    f"unit '{name}' references missing repo '{repo_name}'",
                    f"units[{idx}].repos",
                )
            )

    return issues


def render_validation(issues: list[ValidationIssue]) -> str:
    if not issues:
        return "WorkspaceSpec\n- valid\n"
    lines = ["WorkspaceSpec", "LEVEL\tCODE\tPATH\tMESSAGE"]
    for issue in issues:
        lines.append(f"{issue.level}\t{issue.code}\t{issue.path or '-'}\t{issue.message}")
    return "\n".join(lines)


def build_plan(workspace_root: Path) -> tuple[dict[str, object], list[PlanOperation]]:
    issues = validate_spec(workspace_root)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        rendered = "\n".join(f"- {issue.message}" for issue in errors)
        message = f"workspace spec validation failed:\n{rendered}"
        # Printed AND raised, so the message is on stderr in-process as well as at
        # the interpreter's exit handler. The `unit_path_not_yet_appliable` branch
        # that used to be the only printer went away with the refusal it served,
        # and its removal silently moved every OTHER validation failure's message
        # off stderr -- caught by
        # `test_two_units_colliding_on_one_sanitised_path_are_refused`, which reads
        # `capsys.readouterr().err` and got an empty string.
        print(message, file=sys.stderr)
        raise SystemExit(1)

    spec = load_workspace_spec_doc(workspace_root)
    operations: list[PlanOperation] = []

    for repo in spec.get("repos", []):
        repo_name = str(repo["name"])
        repo_path = workspace_root / str(repo["path"])
        cache_path = repo_cache_path(workspace_root, repo_name)
        if not cache_path.exists():
            operations.append(
                PlanOperation(
                    kind="seed_repo_cache",
                    subject=repo_name,
                    target_path=str(cache_path),
                    reason="repo cache missing",
                    details={"url": str(repo["url"])},
                )
            )
        if not repo_path.exists():
            operations.append(
                PlanOperation(
                    kind="clone_repo",
                    subject=repo_name,
                    target_path=str(repo_path),
                    reason="repo path missing",
                    details={"url": str(repo["url"]), "cache_path": str(cache_path)},
                )
            )

    for unit in spec.get("units", []):
        unit_name = str(unit["name"])
        # Resolved, not joined: an uncontained path here became a mkdir and a
        # unit.toml write outside the root.
        unit_home = unit_root(workspace_root, unit)
        # NOT `<unit_home>/unit.toml`: for a sibling desk that path is another
        # agent's checkout. See `unit_metadata_path`.
        unit_toml = unit_metadata_path(workspace_root, unit)
        if not unit_home.exists():
            operations.append(
                PlanOperation(
                    kind="create_unit_root",
                    subject=unit_name,
                    target_path=str(unit_home),
                    reason="unit path missing",
                    details={"repos": [str(repo) for repo in unit.get("repos", [])]},
                )
            )
        if not unit_toml.exists():
            operations.append(
                PlanOperation(
                    kind="write_unit_metadata",
                    subject=unit_name,
                    target_path=str(unit_toml),
                    reason="unit metadata missing",
                    details={"repos": [str(repo) for repo in unit.get("repos", [])]},
                )
            )

        # grip#539: computed unconditionally, not gated on unit_root/unit_toml
        # already existing. A brand-new unit's declared repos are trivially
        # "missing" too (unit_root doesn't exist yet, so the member path
        # does not exist for every r) -- the old guard meant a first apply published
        # the unit shell without scheduling its clones, requiring a second,
        # separate apply to notice. Ordered after create_unit_root/
        # write_unit_metadata in this loop, so apply_plan's execution (which
        # processes operations in list order) creates the directory before
        # trying to clone into it.
        #
        # THE MEMBER'S SPEC PATH, through the one resolver, so this site cannot
        # disagree with the read verbs (section 6c item 3). Before that, a desk
        # holding the member at its declared path read as missing here and the
        # plan cloned a second copy of it beside the agent's own checkout.
        declared_repos = [str(r) for r in unit.get("repos", [])]
        missing_repos = [
            r
            for r in declared_repos
            if not unit_member_path(workspace_root, spec, unit, r).exists()
        ]
        if missing_repos:
            operations.append(
                PlanOperation(
                    kind="converge_unit_repos",
                    subject=unit_name,
                    target_path=str(unit_home),
                    reason=f"missing repo checkouts: {', '.join(missing_repos)}",
                    details={"missing_repos": missing_repos, "all_repos": declared_repos},
                )
            )

    return spec, operations


def render_plan(operations: list[PlanOperation]) -> str:
    if not operations:
        return "ExecutionPlan\n- no changes required\n"
    lines = ["ExecutionPlan", "KIND\tSUBJECT\tTARGET\tREASON"]
    for op in operations:
        lines.append(f"{op.kind}\t{op.subject}\t{op.target_path}\t{op.reason}")
    return "\n".join(lines)


def _emit_projected(workspace_root: Path, repo: str, projection: dict[str, object]) -> None:
    """The ONE emit site for workspace.file_projected: the clone-op pass and
    the pending first-materialize pass both report through it, so the
    outcome-policy counter sees a single classified call site."""
    emit_after_outcome(
        event_type=EventType.WORKSPACE_FILE_PROJECTED,
        workspace_root=workspace_root,
        actor="system",
        owner_unit="workspace",
        payload={
            "repo": repo,
            "kind": projection["kind"],
            "src": projection["src"],
            "dest": projection["dest"],
        },
    )


def apply_plan(workspace_root: Path, *, yes: bool, manual_hooks: bool = False) -> dict[str, object]:
    spec, operations = build_plan(workspace_root)
    if len(operations) > 3 and not yes:
        # The refusal carries the plan it is refusing: a stranger without
        # --yes saw only the operation count and had to guess what would run.
        raise SystemExit(
            "plan contains more than 3 operations; rerun with --yes to apply it.\n"
            + render_plan(operations)
        )

    # Local import: clone_exec imports THIS module, so the dependency runs the
    # other way at module scope. The helper lives there because rmtree_or_refuse
    # does, and a second cleanup implementation is what its own structural test
    # forbids. It is bound ONCE, here, because both branch arms below use it --
    # importing it inside the first arm left `converge_unit_repos` reading an
    # unbound name, and the failure surfaced as an exit 1 with empty stdout.
    from .clone_exec import clone_and_pin

    applied: list[str] = []
    materialized_repos: list[dict[str, object]] = []
    for op in operations:
        if op.kind == "clone_repo":
            repo_spec = _find_repo(spec, op.subject)
            repo_root = workspace_root / str(repo_spec["path"])
            cache_path = repo_cache_path(workspace_root, str(repo_spec["name"]))
            first_materialize = clone_and_pin(
                str(repo_spec["url"]),
                repo_root,
                pin=str(repo_spec.get("pin") or ""),
                member=str(repo_spec["name"]),
                reference_repo_root=cache_path,
            )
            hook_payload = _run_materialize_hooks(
                workspace_root,
                repo_root,
                str(repo_spec["name"]),
                first_materialize,
                manual_hooks=manual_hooks,
            )
            for projection in hook_payload["projected_files"]:
                _emit_projected(workspace_root, str(repo_spec["name"]), projection)
            materialized_repos.append({"repo": str(repo_spec["name"]), "first_materialize": first_materialize})
            applied.append(f"cloned repo '{op.subject}' into {repo_root}")
        elif op.kind == "seed_repo_cache":
            repo_spec = _find_repo(spec, op.subject)
            cache_path = repo_cache_path(workspace_root, str(repo_spec["name"]))
            created = ensure_repo_cache(str(repo_spec["url"]), cache_path)
            if created:
                applied.append(f"seeded repo cache for '{op.subject}' at {cache_path}")
            else:
                applied.append(f"refreshed repo cache for '{op.subject}' at {cache_path}")
        elif op.kind == "create_unit_root":
            # NOT named `unit_root`: that is the resolver, and rebinding it here
            # made the name a local for this whole function, so the calls below
            # hit a Path where a function was expected.
            unit_home = Path(op.target_path)
            unit_home.mkdir(parents=True, exist_ok=True)
            applied.append(f"created unit root for '{op.subject}' at {unit_home}")
        elif op.kind == "write_unit_metadata":
            unit_spec = _find_unit(spec, op.subject)
            # The metadata's parent, not the unit home: for a sibling desk the
            # metadata lives under the root's `.grip/state/units/<unit>/`, and
            # creating the unit home here would put a directory INSIDE a desk the
            # plan may have decided not to touch at all.
            unit_toml = unit_metadata_path(workspace_root, unit_spec)
            unit_toml.parent.mkdir(parents=True, exist_ok=True)
            unit_toml.write_text(render_unit_toml(unit_spec))
            applied.append(f"wrote unit metadata for '{op.subject}' at {unit_toml}")
        elif op.kind == "converge_unit_repos":
            unit_spec = _find_unit(spec, op.subject)
            missing = [str(r) for r in op.details.get("missing_repos", [])]
            converged: list[str] = []
            for repo_name in missing:
                repo_spec = _find_repo(spec, repo_name)
                # THE MEMBER'S SPEC PATH, through the one resolver. A name-keyed
                # join cloned into `<unit home>/<name>` and left the desk holding
                # two copies of one member: the agent's own at its path and gr2's
                # beside it.
                clone_dest = unit_member_path(workspace_root, spec, unit_spec, repo_name)
                cache_path = repo_cache_path(workspace_root, str(repo_spec["name"]))
                pin = str(repo_spec.get("pin") or "")
                # A clone lands on the remote's default tip; the root declares a
                # commit. The helper stages, pins, then renames, so a refusal
                # cannot leave a clone behind at the tip for the next run to
                # accept as converged.
                first_materialize = clone_and_pin(
                    str(repo_spec["url"]),
                    clone_dest,
                    pin=pin,
                    member=repo_name,
                    reference_repo_root=cache_path,
                )
                if first_materialize:
                    converged.append(f"{repo_name}@{pin[:12]}" if pin else repo_name)
                    materialized_repos.append({"repo": repo_name, "first_materialize": True})
            unit_toml = unit_metadata_path(workspace_root, unit_spec)
            unit_toml.parent.mkdir(parents=True, exist_ok=True)
            unit_toml.write_text(render_unit_toml(unit_spec))
            applied.append(f"converged unit '{op.subject}': cloned {', '.join(converged)}")
        else:
            raise SystemExit(f"unknown plan operation kind: {op.kind}")

    if applied:
        _record_apply_state(workspace_root, applied)
    if materialized_repos:
        emit_after_outcome(
            event_type=EventType.WORKSPACE_MATERIALIZED,
            workspace_root=workspace_root,
            actor="system",
            owner_unit="workspace",
            payload={"repos": materialized_repos},
        )

    # The pending first-materialize pass: a member
    # whose hooks were skipped while unbound writes a pending marker; the
    # NEXT materialize where the member is BOUND runs the member's hooks
    # once here — first-materialize semantics preserved, never rm -rf. A
    # clone op in THIS run already consumed its marker (the gate consumes it
    # on the bound hook pass), so only still-pending members are served.
    for member_key in pending_members(workspace_root):
        # the pending key is the member's declared PATH; the spec names repos
        # by name — resolve path -> spec row, then use the row's own name.
        repo_spec = next(
            (r for r in spec.get("repos", []) if str(r.get("path", "")).strip("/") == member_key),
            None,
        )
        if repo_spec is None:
            continue
        repo_name = str(repo_spec["name"])
        repo_root = workspace_root / str(repo_spec["path"])
        if not repo_root.is_dir():
            continue
        # serve only BOUND members: while unbound the gate skips again and
        # the marker stays pending; the applied-line must never claim a run
        # that the consent gate refused.
        if consent_state(workspace_root, consent_member_key(workspace_root, repo_root, repo_name), repo_root)[0] != "bound":
            continue
        hook_payload = _run_materialize_hooks(
            workspace_root,
            repo_root,
            repo_name,
            True,  # first-materialize semantics for the deferred hooks
            manual_hooks=manual_hooks,
        )
        for projection in hook_payload["projected_files"]:
            _emit_projected(workspace_root, repo_name, projection)
        applied.append(f"ran pending hooks for '{repo_name}' (bound on a later materialize)")

    return {
        "workspace_root": str(workspace_root),
        "applied": applied,
        "operation_count": len(operations),
    }


def render_apply_result(payload: dict[str, object]) -> str:
    applied = [str(item) for item in payload.get("applied", [])]
    lines = ["ApplyResult", f"workspace_root = {payload['workspace_root']}", f"operation_count = {payload['operation_count']}"]
    if not applied:
        lines.append("- no changes applied")
        return "\n".join(lines)
    lines.append("ACTIONS")
    lines.extend(f"- {item}" for item in applied)
    return "\n".join(lines)


def _find_repo(spec: dict[str, object], repo_name: str) -> dict[str, object]:
    for repo in spec.get("repos", []):
        if str(repo.get("name")) == repo_name:
            return repo
    raise SystemExit(f"repo not found in workspace spec: {repo_name}")


def _find_unit(spec: dict[str, object], unit_name: str) -> dict[str, object]:
    for unit in spec.get("units", []):
        if str(unit.get("name")) == unit_name:
            return unit
    raise SystemExit(f"unit not found in workspace spec: {unit_name}")


def _run_materialize_hooks(
    workspace_root: Path,
    repo_root: Path,
    repo_name: str,
    first_materialize: bool,
    *,
    manual_hooks: bool = False,
) -> dict[str, list[dict[str, object]]]:
    hooks = load_repo_hooks(repo_root)
    if not hooks:
        return {"projected_files": []}
    ctx = HookContext(
        workspace_root=workspace_root,
        unit_root=workspace_root,
        lane_root=repo_root,
        repo_root=repo_root,
        repo_name=repo_name,
        lane_owner="workspace",
        lane_subject=repo_name,
        lane_name="workspace",
    )
    projections = run_materialize_hook_block(
        hooks,
        ctx,
        repo_dirty=repo_dirty(repo_root),
        first_materialize=first_materialize,
        allow_manual=manual_hooks,
    )
    projected_files: list[dict[str, object]] = []
    for result in projections:
        if result.status != "applied" or not result.src or not result.dest:
            continue
        projected_files.append(
            {
                "kind": result.name.split(":", 1)[0],
                "src": _relative_workspace_path(workspace_root, Path(result.src)),
                "dest": _relative_workspace_path(workspace_root, Path(result.dest)),
            }
        )
    return {"projected_files": projected_files}


def _relative_workspace_path(workspace_root: Path, path: Path) -> str:
    return os.path.relpath(path, workspace_root)


def render_unit_toml(unit_spec: dict[str, object]) -> str:
    repos = [str(repo) for repo in unit_spec.get("repos", [])]
    repos_str = "[" + ", ".join(f'"{repo}"' for repo in repos) + "]"
    lines = [
        f'name = "{unit_spec["name"]}"',
        'kind = "unit"',
        f"repos = {repos_str}",
    ]
    return "\n".join(lines) + "\n"


def _record_apply_state(workspace_root: Path, actions: list[str]) -> None:
    state_dir = workspace_root / ".grip" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "applied.toml"
    timestamp = datetime.now(UTC).isoformat()
    content = [
        "[[applied]]",
        f'timestamp = "{timestamp}"',
        "actions = [" + ", ".join(json.dumps(action) for action in actions) + "]",
        "",
    ]
    if state_path.exists():
        existing = state_path.read_text().rstrip()
        state_path.write_text(existing + "\n\n" + "\n".join(content))
    else:
        state_path.write_text("\n".join(content))


# ---------------------------------------------------------------------------
# Neutral MaterializationPlan v1 -- plan contract (S4-A)
#
# This module ships the PLAN-LEVEL contract only: schema conformance,
# identity-freedom, opaque-token safety, WorkspaceSpec binding, path
# canonicalization, destination-collision detection, and durable receipt
# publication. It deliberately ships NO operation execution -- the clone,
# staging/project_file, and venv/editable handlers arrive in S4-B/C/D,
# each with its own domain validation and mutation set (grip#797 split).
#
# Carve rationale: every guarantee here is enforced before any handler
# could run, so it is reviewable and complete on its own, and landing it
# first cannot ship a half-hardened operation path.
# ---------------------------------------------------------------------------


class MaterializationPlanError(Exception):
    pass


# Capability seal. A ValidatedPlan can only be minted by
# validate_materialization_plan, so a receipt cannot be published from a
# plan that was never validated: the writer previously accepted
# the raw live plan and an arbitrary result list, which let a schema-invalid
# plan_id escape the receipt directory and let an unvalidated result graph
# be persisted verbatim.
#
# This was an opaque sentinel object, keyed on IDENTITY. Sentinel's witness:
# dataclasses.replace() re-invokes __init__ with the existing field values,
# so the real token rode into a modified shell and publication used the
# altered plan_id -- writing .grip/escaped.json outside the receipt
# directory. Identity is copyable; the capability has to bind CONTENT.
#
# Process-local and never persisted: this is an in-process capability, not a
# credential. It cannot be recomputed by a caller who did not go through
# validation, which is the whole point.
_CAPABILITY_SECRET = secrets.token_bytes(32)


def _deep_freeze(value: object) -> object:
    """Recursively convert a JSON-shaped graph into a read-only one.

    dataclasses.dataclass(frozen=True) freezes the field BINDING, not the
    graph the field points at -- so a `plan` field holding a live dict is
    mutable through anyone who holds the capability. Mappings become
    MappingProxyType and sequences become tuples, which closes both the
    item-assignment and the append/extend routes."""
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


# Provenance registry: the instances this validator actually minted.
#
# Orthogonal to the seal, and deliberately so. The seal binds CONTENT, which
# closes replace() and in-place edits; provenance binds ORIGIN, which closes
# copying that preserves authority without changing any field. They fail in
# different ways, which is what makes them defense in depth rather than one
# guard shadowing another.
#
# Weak, so holding a capability never keeps it alive; entries vanish with the
# object. eq=False on the dataclass keeps identity hashing (a MappingProxyType
# field is unhashable anyway) -- and two capabilities over equal facts SHOULD
# be distinct capabilities, which is exactly what identity semantics give.
_MINTED_CAPABILITIES: weakref.WeakSet = weakref.WeakSet()


@dataclasses.dataclass(frozen=True, eq=False)
class ValidatedPlan:
    """Proof that a plan passed the full v1 contract, plus the facts a
    publisher needs to bind its evidence to that plan.

    Immutable and unforgeable-by-accident: the token check means holding one
    of these IS the evidence of validation, so publication has a capability
    to demand rather than a convention to trust.

    Every field here is a mint-time CAPTURE, not a view onto something a
    caller still holds (Atlas final re-gate). The earlier version aliased the
    caller's dict and recomputed the hash at publication time, so mutating
    the plan after validation produced a receipt attesting to a graph that
    was never validated -- the capability proved one graph had been checked
    while vouching for another. Publication now reads captured facts only."""

    plan: MappingProxyType
    plan_id: str
    unit_key: str
    schema_version: int
    workspace_spec_sha256: str
    operation_kinds: tuple[str, ...]
    plan_hash: str
    _seal: str = dataclasses.field(repr=False)

    def __post_init__(self) -> None:
        # Provenance cannot be checked here: registration happens after
        # construction, so this instance is not in the registry yet.
        self.verify()

    def verify(self, *, require_provenance: bool = False) -> None:
        """Re-derive the seal from the current contents and compare.

        Called at construction AND at every use. Construction-time checking
        alone is not enough: `frozen=True` blocks __setattr__, not
        object.__setattr__, so an already-minted capability can still be
        edited in place. Re-deriving at use means the fields a publisher
        reads are provably the fields that were sealed.

        `require_provenance` adds the origin check, which only makes sense at
        USE. Copying a capability preserves every field and therefore every
        content check; only origin can refuse it."""
        # The hash must actually describe the snapshot, so swapping the graph
        # (with or without a matching hash) cannot survive.
        if compute_plan_hash(self.plan) != self.plan_hash:
            raise MaterializationPlanError(
                "ValidatedPlan capability is invalid: plan_hash does not describe its plan snapshot"
            )
        # ...and the snapshot must agree with every fact published beside it,
        # so the two can never diverge into "sealed but inconsistent".
        snapshot_facts = (
            self.plan.get("plan_id"),
            self.plan.get("unit_key"),
            self.plan.get("schema_version"),
            self.plan.get("workspace_spec_sha256"),
            tuple(str(op.get("kind")) for op in self.plan.get("operations", ())),
        )
        if snapshot_facts != (
            self.plan_id,
            self.unit_key,
            self.schema_version,
            self.workspace_spec_sha256,
            tuple(self.operation_kinds),
        ):
            raise MaterializationPlanError(
                "ValidatedPlan capability is invalid: published facts disagree with the plan snapshot"
            )
        expected = _capability_seal(
            plan_hash=self.plan_hash,
            plan_id=self.plan_id,
            unit_key=self.unit_key,
            schema_version=self.schema_version,
            workspace_spec_sha256=self.workspace_spec_sha256,
            operation_kinds=self.operation_kinds,
        )
        if not hmac.compare_digest(str(self._seal), expected):
            raise MaterializationPlanError(
                "ValidatedPlan capability is invalid: it was not minted by "
                "validate_materialization_plan for these exact facts"
            )
        if require_provenance and self not in _MINTED_CAPABILITIES:
            raise MaterializationPlanError(
                "ValidatedPlan capability is invalid: it is not one this validator "
                "minted -- a copy preserves every field but not its provenance"
            )

    def consume(self, op_results: object) -> _ConsumptionBinding:
        """Verify once, then take ONE immutable reading of everything a
        publisher will use.

        This closes the check/use window Atlas found. Verifying and then
        re-reading `self.plan_id` to build a filename is a live read: a
        caller-controlled callback (his was `list.__iter__` on the evidence)
        runs in between and mutates the shell, so the receipt is written
        under an identity that was never verified.

        Two details carry the fix:

        The facts come from `self.plan`, the FROZEN snapshot, not from the
        shell fields. The snapshot cannot be mutated at all, so deriving from
        it makes a post-verify shell edit irrelevant rather than merely
        detected. There is deliberately no second seal check after the
        callback -- it would be unreachable, and an unreachable guard is
        worse than none because it reads as protection while being untestable
        at its own level.

        The ORDER is load-bearing: facts are captured before the evidence is
        materialized, because materializing is what runs caller code."""
        self.verify(require_provenance=True)

        # Captured first -- no caller code has run yet at this point.
        plan = self.plan
        facts = {
            "plan_id": str(plan["plan_id"]),
            "unit_key": str(plan["unit_key"]),
            "schema_version": int(plan["schema_version"]),
            "workspace_spec_sha256": str(plan["workspace_spec_sha256"]),
            "operation_kinds": tuple(str(op["kind"]) for op in plan["operations"]),
            "plan_hash": compute_plan_hash(plan),
        }

        # ...and only now touch the caller's object, exactly once.
        if not isinstance(op_results, list):
            raise MaterializationPlanError("receipt evidence must be a list of operation results")
        return _ConsumptionBinding(evidence=tuple(_plain_json(list(op_results))), **facts)


def _plain_json(value: object) -> object:
    """Materialize a caller-supplied graph into plain JSON types, once.

    Two jobs. It detaches the value from anything the caller still holds, and
    it normalizes away container subclasses -- so nothing downstream, json
    serialization included, can re-enter caller code and get a second,
    different answer."""
    if isinstance(value, (dict, MappingProxyType)):
        return {str(k): _plain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(v) for v in value]
    return value


@dataclasses.dataclass(frozen=True)
class _ConsumptionBinding:
    """The single immutable reading taken at the verified consumption
    instant (Atlas's use-time closure).

    Every field a publisher needs, captured together, so nothing downstream
    ever reads the live capability shell or the caller's evidence again."""

    plan_id: str
    unit_key: str
    schema_version: int
    workspace_spec_sha256: str
    operation_kinds: tuple[str, ...]
    plan_hash: str
    evidence: tuple


def _capability_seal(
    *,
    plan_hash: str,
    plan_id: str,
    unit_key: str,
    schema_version: int,
    workspace_spec_sha256: str,
    operation_kinds: tuple[str, ...],
) -> str:
    """Bind the seal to CONTENT, not to object identity.

    Every fact publication consumes is covered, so altering any one of them
    -- by dataclasses.replace, by object.__setattr__, or by hand-building a
    shell -- produces a seal that no longer matches. Takes values rather than
    an instance so the mint can compute it before the object exists."""
    payload = json.dumps(
        [
            plan_hash,
            plan_id,
            unit_key,
            schema_version,
            workspace_spec_sha256,
            list(operation_kinds),
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(_CAPABILITY_SECRET, payload, hashlib.sha256).hexdigest()


# The normative MaterializationPlan v1 wire contract. A hand-rolled
# validator is a separate, looser contract by construction -- nine plans the
# pinned schema rejects were accepted by an earlier hand version, and
# schema_version=True slipped a `!= 1` check because bool is an int subclass
# in Python (True == 1) while JSON Schema's const:1 distinguishes the types.
# The packaged bytes are verified against the pinned SHA at load and FAIL
# CLOSED, so a tampered or unpinned schema refuses to validate at all rather
# than silently enforcing something else.
_PLAN_SCHEMA_SHA256 = "a5061501ba6651d7432d87d57f1c85902e5dec076f860a47faa299f5f590231c"
_PLAN_SCHEMA_RESOURCE = "schemas/gr2-materialization-plan-v1.schema.json"
_plan_validator: Draft202012Validator | None = None

_VALID_OPERATION_KINDS = frozenset({"clone", "venv", "editable_install", "project_file"})


def _read_plan_schema_bytes() -> bytes:
    """importlib.resources is the real (installed) path; the sibling-directory
    fallback covers the in-repo pytest context, where conftest.py injects a
    bare `gr2` module without a __spec__ and resource traversal fails. Either
    way the bytes are SHA-verified before use, so WHERE they load from cannot
    weaken WHAT gets enforced."""
    try:
        return (importlib.resources.files("gr2") / _PLAN_SCHEMA_RESOURCE).read_bytes()
    except Exception:
        return (Path(__file__).resolve().parent.parent / _PLAN_SCHEMA_RESOURCE).read_bytes()


def _load_plan_validator() -> Draft202012Validator:
    global _plan_validator
    if _plan_validator is None:
        raw = _read_plan_schema_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != _PLAN_SCHEMA_SHA256:
            raise MaterializationPlanError(
                f"packaged MaterializationPlan v1 schema hash mismatch: expected "
                f"{_PLAN_SCHEMA_SHA256}, got {actual} -- refusing to validate against "
                "an unpinned schema"
            )
        _plan_validator = Draft202012Validator(json.loads(raw))
    return _plan_validator


# MaterializationPlan v1: "The production plan must not contain: agent display
# name, persistent agent ID, role, org or project, channel, entitlement
# result or reason, secret reference or value, memory body." Checked
# recursively as defence in depth -- the per-kind ALLOWLIST below is what
# actually proves identity-freedom, since a blacklist only catches names
# someone thought to enumerate.
_FORBIDDEN_IDENTITY_KEYS = frozenset(
    {
        "agent_name",
        "agent_id",
        "persistent_identity_ref",
        "role",
        "org",
        "project",
        "channel",
        "channels",
        "entitlement",
        "entitlement_reason",
        "secret",
        "secret_ref",
        "memory",
        "memory_body",
    }
)


def _reject_identity_fields_recursive(value: object, *, path: str) -> None:
    if isinstance(value, dict):
        present = _FORBIDDEN_IDENTITY_KEYS & value.keys()
        if present:
            raise MaterializationPlanError(
                f"{path} carries identity-bearing field(s) {sorted(present)}; "
                "gr2 MaterializationPlan operations must be identity-free"
            )
        for key, nested in value.items():
            _reject_identity_fields_recursive(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _reject_identity_fields_recursive(item, path=f"{path}[{i}]")


def _validate_path_safe_token(value: object, *, field_name: str) -> str:
    """An opaque, path-safe identifier: no separators, no traversal, no
    identity semantics interpreted. plan_id and unit_key both end up embedded
    in filesystem paths (receipt filenames), so they are validated as tokens
    -- gr2 never derives identity from unit_key, it only checks its shape."""
    if not isinstance(value, str) or not value:
        raise MaterializationPlanError(f"{field_name} must be a non-empty string")
    if "/" in value or "\\" in value or value in (".", "..") or "\x00" in value:
        raise MaterializationPlanError(f"{field_name} must be a path-safe token, got {value!r}")
    return value


def unit_root(workspace_root: Path, unit: dict) -> Path:
    """Resolve one unit's home from its spec `path`, one of exactly THREE forms.

    A unit path is:
      - a root-relative path INSIDE the root (the nested default);
      - `"."` — the unit works in the root itself (gr1 `worktree = "main"`);
      - exactly `"../<single-component>"` — a gr1 SIBLING desk, which gr1 keeps
        beside the gripspace root as `<parent>/<workspace>-<agent>/`.

    Everything else is refused: absolute paths, `~`, backslashes, NUL, `../../x`,
    `../x/y`, an empty or `.`/`..` sibling component, and any path whose existing
    prefix holds a symlink — so a link cannot walk the real bytes outside the
    root while the textual path still reads as inside it.

    This is deliberately NOT a call to `canonicalize_workspace_path` on
    `workspace_root`: that helper refuses every `..` segment, and a sibling unit
    path IS one `..` followed by a single name. The containment work is still
    delegated to it — for the sibling form the base is the PARENT directory, so
    the same per-component lstat walk applies and only the base differs.
    """
    raw = unit.get("path")
    field = f"unit {unit.get('name')!r} path"
    if not isinstance(raw, str) or not raw.strip():
        raise MaterializationPlanError(f"{field} must be a non-empty string")
    path = raw.strip()

    if path == ".":
        # The desk whose worktree IS the root. Resolved so comparison against
        # other resolved paths cannot disagree on a symlinked spellings.
        return workspace_root.resolve()

    if path.startswith("../"):
        component = path[len("../"):]
        # Exactly one component, and not a second `..` — so `../../x` and
        # `../x/y` are refused here rather than silently resolved.
        if component in ("", ".", "..") or "/" in component:
            raise MaterializationPlanError(
                f"{field} may name at most one sibling (`../<component>`): {path!r}"
            )
        return canonicalize_workspace_path(
            workspace_root.parent, component, field_name=field
        )

    # Root-relative: the nested default, and the only form with no `..` at all.
    return canonicalize_workspace_path(workspace_root, path, field_name=field)


# The one-release alpha fallback prints its line ONCE per (checkout, declared
# path) pair even though a plan-then-apply verb resolves the same member twice in
# one process. Process-local on purpose: a CLI invocation is one process, and the
# key is absolute, so two workspaces in one run still each get their own line.
_ALPHA_FALLBACK_NOTICED: set[tuple[str, str]] = set()


def _member_spec_path(spec: dict, member: str) -> str | None:
    """The member's declared `path` in this spec, or None when it declares no such member."""
    for repo in spec.get("repos", []):
        if str(repo.get("name")) == member:
            declared = str(repo.get("path", "")).strip()
            return declared or None
    return None


def unit_metadata_path(workspace_root: Path, unit: dict) -> Path:
    """Where one unit's `unit.toml` is written — the TWO homes, and why they differ.

    A unit INSIDE the root keeps `<unit home>/unit.toml`: that is where every
    existing workspace has it, and moving it would churn workspaces that have no
    problem. A unit AT the root (`"."`, gr1 `worktree = "main"`) or in a SIBLING
    desk (`"../x"`) gets `<root>/.grip/state/units/<unit>/unit.toml` instead.

    The sibling case is the reason the rule exists: a desk beside the root is
    another agent's checkout, and a gr2 file dropped into it is the same class of
    pollution that moved lanes out of `agents/`. The metadata is gr2's, so it
    lives under gr2's own state root.
    """
    raw = str(unit.get("path") or "").strip()
    if raw == "." or raw.startswith("../"):
        return (
            workspace_root
            / ".grip"
            / "state"
            / "units"
            / str(unit.get("name"))
            / "unit.toml"
        )
    return unit_root(workspace_root, unit) / "unit.toml"


def unit_member_path(workspace_root: Path, spec: dict, unit: dict, member: str) -> Path:
    """One unit's copy of `member`, at the MEMBER'S SPEC PATH, refusing an illegal
    unit path in a sentence.

    THE PATH, NOT THE NAME (section 6c item 3). A gr1 desk places its repos by the
    path the manifest declares, and name and path are two different coordinates:
    13 of 25 live repos have a name different from their path. A name-keyed join
    looks for `<desk>/synapt-config` where the desk holds `<desk>/config`, plans a
    clone of a member that is already there, and would drop a second copy beside
    the agent's own checkout.

    THE ONE-RELEASE ALPHA FALLBACK, and it reads rather than acts. gr2 alpha (a5
    is on PyPI) placed members by NAME, so an alpha user whose member name differs
    from its path may have their checkout at `<unit home>/<name>`. When nothing
    exists at the path but a checkout exists at the name, this reads it there,
    prints ONE line naming both, and neither moves nor clones over it. Nothing is
    moved automatically; the release note names the change.

    The READ verbs (lane materialization, the store member map) walk a spec they
    did not write, so they meet `unit_root`'s grammar as a refusal they cannot
    see coming. Left untranslated that refusal reaches the user as a Python
    traceback standing in the place of the sentence it already is — the one
    thing this package refuses to do elsewhere.
    """
    try:
        unit_home = unit_root(workspace_root, unit)
    except MaterializationPlanError as exc:
        raise SystemExit(f"the workspace spec is refused: {exc}") from None
    declared = _member_spec_path(spec, member)
    at_name = unit_home / member
    if declared is None:
        # A spec that names no such member: the name IS the only coordinate left.
        return at_name.resolve()
    at_path = unit_home / declared
    if at_path.exists():
        # PATH WINS, INCLUDING WHEN IT IS EMPTY, and that half is deliberate: an
        # empty directory at a declared path is `empty_placeholder`, the ordinary
        # state of a freshly cloned superproject, and `validate_spec` blesses it
        # rather than calling it a conflict.
        #
        # WHAT WAS NOT DELIBERATE IS THE SILENCE. `gitops.repo_path_state` exists
        # in three answers precisely because "present" is not one question: the
        # planner's missing-member test is `unit_member_path(...).exists()`, so an
        # EMPTY directory at the declared path reads as PRESENT and no clone is
        # planned -- while a real checkout from an earlier gr2 release sits at the
        # name one directory over, which is the exact confusion this note exists
        # for. It did not fire. Measured before this line: path holding an empty
        # dir with a real `.git` checkout at the name returned the PATH and
        # printed nothing, so the member reads as present, is empty, and the user
        # has no way to see either.
        #
        # The RESOLUTION is unchanged (see the paragraph above: a placeholder is a
        # legal destination and the sync planner clones into one). Only the
        # silence is removed, so this cannot alter which member a verb reads.
        if at_name.exists() and gitops.repo_path_state(at_path) != "repo_root":
            _note_alpha_checkout(member, declared, at_name, at_path, empty_at_path=True)
        return at_path.resolve()
    if not at_name.exists():
        return at_path.resolve()
    _note_alpha_checkout(member, declared, at_name, at_path, empty_at_path=False)
    return at_name.resolve()


def _note_alpha_checkout(
    member: str, declared: str, at_name: Path, at_path: Path, *, empty_at_path: bool
) -> None:
    """The one-release note, at most once per (name, path) pair.

    EXACTLY ONE LINE, which is the design's word for it and is not free:
    `apply_plan` calls `build_plan`, and a verb that plans and then applies
    resolves the same member twice in one process, so a bare print emits the
    note twice for one user action. Keyed on the two absolute paths, so two
    different members, or one member in two workspaces, each still get their
    own line.
    """
    key = (str(at_name), str(at_path))
    if key in _ALPHA_FALLBACK_NOTICED:
        return
    _ALPHA_FALLBACK_NOTICED.add(key)
    if empty_at_path:
        print(
            f"note: '{member}' resolves to {at_path}, which holds no checkout, while a "
            f"checkout built by an earlier gr2 release sits at {at_name}. Nothing is "
            f"moved and nothing is cloned over it; the spec declares '{declared}', so "
            f"the path is what is read. If {at_name} is the checkout you meant, it is "
            "not the one this verb is reading.",
            file=sys.stderr,
        )
        return
    print(
        f"note: '{member}' is checked out at {at_name}, the name gr2 placed members "
        f"by until this release, while the workspace spec declares its path as "
        f"{declared!r} ({at_path}). Reading the existing checkout where it is; "
        "nothing is moved and nothing is cloned over it.",
        file=sys.stderr,
    )


def canonicalize_workspace_path(workspace_root: Path, relative: str, *, field_name: str) -> Path:
    """MaterializationPlan v1 invariant #2: reject absolute paths, `~`,
    backslashes, empty segments, `.` or `..` segments, NUL, any existing
    symlink in the path prefix, and any resolved escape.

    Segment checks run on the RAW string split on "/" -- Path() silently
    normalizes single-dot segments away (Path("a/./b").parts == ("a","b")),
    so parts-based scanning cannot see them.

    The per-component symlink walk (lstat on each existing component,
    including the last) is what a resolve()-based containment check
    structurally cannot provide: if a directory in the prefix is itself a
    symlink, BOTH the candidate and the root resolve through that same link,
    so "resolved candidate is under resolved root" holds while the real bytes
    live outside.

    Returns the fully resolved canonical path -- used for filesystem access
    AND for all comparison (collision detection), so two spellings of one
    real path cannot both pass."""
    if not relative or relative.startswith("/") or relative.startswith("~"):
        raise MaterializationPlanError(f"{field_name} must be relative to the workspace root: {relative!r}")
    if "\\" in relative or "\x00" in relative:
        raise MaterializationPlanError(f"{field_name} must not contain backslashes or NUL: {relative!r}")
    segments = relative.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise MaterializationPlanError(
            f"{field_name} must not contain empty, '.', or '..' segments: {relative!r}"
        )
    walker = workspace_root
    for seg in segments:
        walker = walker / seg
        if walker.is_symlink():
            raise MaterializationPlanError(
                f"{field_name} passes through a symlink at {walker} -- path prefixes must be "
                "symlink-free (MaterializationPlan v1 invariant #2)"
            )
    workspace_resolved = workspace_root.resolve()
    candidate = (workspace_root / relative).resolve()
    if candidate != workspace_resolved and workspace_resolved not in candidate.parents:
        raise MaterializationPlanError(f"{field_name} escapes the workspace root: {relative!r}")
    return candidate


# Exact per-kind ALLOWLISTS. Any field not explicitly permitted for its kind
# is rejected by construction -- which is what proves identity-freedom, since
# a field nobody thought to blacklist (display_name, say) cannot exist at all.
_CLONE_FIELDS = frozenset({"kind", "repo_url", "dest_path", "branch", "reference_base"})
_VENV_FIELDS = frozenset({"kind", "dest_path", "engine", "python"})
_EDITABLE_INSTALL_FIELDS = frozenset({"kind", "venv_path", "source_path", "extras"})
_PROJECT_FILE_FIELDS = frozenset({"kind", "source_path", "dest_path", "source_sha256", "mode"})

_OPERATION_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "clone": _CLONE_FIELDS,
    "venv": _VENV_FIELDS,
    "editable_install": _EDITABLE_INSTALL_FIELDS,
    "project_file": _PROJECT_FILE_FIELDS,
}

_PLAN_ALLOWED_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "plan_id", "unit_key", "workspace_spec_sha256", "operations"}
)


def _require_str(op: dict[str, object], key: str, prefix: str) -> str:
    value = op.get(key)
    if not isinstance(value, str) or not value:
        raise MaterializationPlanError(f"{prefix}: {key} must be a non-empty string")
    return value


def _validate_operation_shape(
    op: dict[str, object], *, idx: int, workspace_root: Path
) -> Path | None:
    """Plan-level shape + path safety for one operation. Filesystem
    PROVENANCE (is that cache a real bare repo with the right origin, is
    that staged input a regular file hashing to source_sha256) belongs to
    the handler that consumes it and lands with S4-B/C/D. The pinned schema
    already constrains reference_base and source_path syntactically.

    Returns the canonical dest_path for collision detection, or None."""
    kind = op.get("kind")
    prefix = f"operations[{idx}] (kind={kind!r})"
    if kind not in _VALID_OPERATION_KINDS:
        raise MaterializationPlanError(f"{prefix}: unknown operation kind")

    unknown = op.keys() - _OPERATION_ALLOWED_FIELDS[kind]
    if unknown:
        raise MaterializationPlanError(f"{prefix}: unknown field(s) {sorted(unknown)}")

    if kind == "clone":
        _require_str(op, "repo_url", prefix)
        _require_str(op, "branch", prefix)
        dest_path = _require_str(op, "dest_path", prefix)
        reference_base = op.get("reference_base")
        if reference_base is not None:
            if not isinstance(reference_base, str) or not reference_base:
                raise MaterializationPlanError(f"{prefix}: reference_base must be a non-empty string")
            canonicalize_workspace_path(
                workspace_root, reference_base, field_name=f"{prefix}.reference_base"
            )
        return canonicalize_workspace_path(workspace_root, dest_path, field_name=f"{prefix}.dest_path")
    if kind == "venv":
        dest_path = _require_str(op, "dest_path", prefix)
        # No defaults anywhere: the pinned schema requires engine and python
        # explicitly, and defaulting here would re-open the coercion the
        # contract closed.
        if op.get("engine") != "uv":
            raise MaterializationPlanError(f"{prefix}: engine must be 'uv', got {op.get('engine')!r}")
        _require_str(op, "python", prefix)
        return canonicalize_workspace_path(workspace_root, dest_path, field_name=f"{prefix}.dest_path")
    if kind == "editable_install":
        venv_path = _require_str(op, "venv_path", prefix)
        source_path = _require_str(op, "source_path", prefix)
        canonicalize_workspace_path(workspace_root, venv_path, field_name=f"{prefix}.venv_path")
        canonicalize_workspace_path(workspace_root, source_path, field_name=f"{prefix}.source_path")
        extras = op.get("extras")
        if not isinstance(extras, list) or not all(isinstance(e, str) for e in extras):
            raise MaterializationPlanError(f"{prefix}: extras must be a list of strings (required)")
        return None
    # project_file
    source_path = _require_str(op, "source_path", prefix)
    dest_path = _require_str(op, "dest_path", prefix)
    _require_str(op, "source_sha256", prefix)
    canonicalize_workspace_path(workspace_root, source_path, field_name=f"{prefix}.source_path")
    if op.get("mode") != "copy":
        raise MaterializationPlanError(f"{prefix}: mode must be 'copy' for v1, got {op.get('mode')!r}")
    return canonicalize_workspace_path(workspace_root, dest_path, field_name=f"{prefix}.dest_path")


def validate_materialization_plan(workspace_root: Path, plan: dict[str, object]) -> ValidatedPlan:
    """Validate a neutral MaterializationPlan against its pinned v1
    contract. Raises MaterializationPlanError on the first violation.

    Validate-before-touch: this runs to completion over the WHOLE plan
    before any handler executes, so an invalid operation late in the list
    cannot let an earlier one mutate state first.

    Returns a ValidatedPlan capability. Publication requires one, so a
    receipt cannot be written from a plan that never passed this function
    -- validation becomes something the publisher HOLDS rather
    than something a caller is trusted to have remembered to do.

    The caller's object is snapshotted on entry and never consulted again.
    Taking the snapshot FIRST (rather than copying at mint) also closes the
    window where a concurrent mutation could land between the checks and the
    capture, which would validate one graph and bind another.
    """
    try:
        plan = copy.deepcopy(plan)
    except Exception as exc:  # pragma: no cover - defensive
        raise MaterializationPlanError(
            f"plan could not be snapshotted for validation: {exc}"
        ) from exc

    validator = _load_plan_validator()
    schema_errors = sorted(validator.iter_errors(plan), key=lambda e: list(e.absolute_path))
    if schema_errors:
        first = schema_errors[0]
        location = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise MaterializationPlanError(
            f"plan rejected by pinned MaterializationPlan v1 schema at {location}: {first.message}"
        )

    unknown_top_level = plan.keys() - _PLAN_ALLOWED_TOP_LEVEL_FIELDS
    if unknown_top_level:
        raise MaterializationPlanError(f"plan: unknown top-level field(s) {sorted(unknown_top_level)}")

    if isinstance(plan.get("schema_version"), bool) or plan.get("schema_version") != 1:
        raise MaterializationPlanError(
            f"plan.schema_version must be exactly 1, got {plan.get('schema_version')!r}"
        )

    _validate_path_safe_token(plan.get("plan_id"), field_name="plan_id")
    # One MaterializationPlan is scoped to one opaque unit and carries a
    # required top-level unit_key. gr2 validates its shape without deriving
    # identity from it.
    _validate_path_safe_token(plan.get("unit_key"), field_name="unit_key")

    # MaterializationPlan v1 invariant #1: reopen the canonical WorkspaceSpec
    # bytes and verify their SHA-256. Carrying the field is not verifying it.
    workspace_spec_sha256 = str(plan["workspace_spec_sha256"])
    actual_spec_sha256 = hashlib.sha256(_read_canonical_workspace_spec_bytes(workspace_root)).hexdigest()
    if actual_spec_sha256 != workspace_spec_sha256:
        raise MaterializationPlanError(
            f"workspace_spec_sha256 mismatch: plan declares {workspace_spec_sha256}, "
            f"canonical WorkspaceSpec bytes hash to {actual_spec_sha256} -- the plan was "
            "compiled against a different workspace state (MaterializationPlan v1 invariant #1)"
        )

    operations = plan["operations"]

    # MaterializationPlan v1 invariant #3: compare destinations after normalization and
    # Unicode-aware case folding. Raw-string comparison lets "u1/.venv" and
    # "u1/./.venv" both through, and casefold (not lower) is required so
    # "straße"/"STRASSE" collide.
    seen_canonical_dests: dict[str, int] = {}
    for idx, op in enumerate(operations):
        if not isinstance(op, dict):
            raise MaterializationPlanError(f"operations[{idx}] must be an object, got {type(op).__name__}")
        _reject_identity_fields_recursive(op, path=f"operations[{idx}]")
        canonical_dest = _validate_operation_shape(op, idx=idx, workspace_root=workspace_root)
        if canonical_dest is not None:
            # NFC-normalize BEFORE casefolding:
            # "units/café/.venv" spelled NFC vs NFD are distinct Python
            # strings that casefold to distinct values, yet on a
            # normalization-insensitive filesystem they name ONE
            # destination. Normalization and case folding are separate
            # aliasing axes; collision detection has to close both.
            key = unicodedata.normalize("NFC", str(canonical_dest)).casefold()
            if key in seen_canonical_dests:
                raise MaterializationPlanError(
                    f"operations[{idx}] dest_path collides (case-folded/normalized) with "
                    f"operations[{seen_canonical_dests[key]}]: {canonical_dest}"
                )
            seen_canonical_dests[key] = idx

    # Hash the validated bytes ONCE, here, while they are still exactly what
    # passed the checks above. Recomputing at publication time is what let a
    # receipt attest to a post-validation mutation.
    facts = {
        "plan_hash": compute_plan_hash(plan),
        "plan_id": str(plan["plan_id"]),
        "unit_key": str(plan["unit_key"]),
        "schema_version": int(plan["schema_version"]),
        "workspace_spec_sha256": workspace_spec_sha256,
        "operation_kinds": tuple(str(op["kind"]) for op in operations),
    }
    validated = ValidatedPlan(
        plan=_deep_freeze(plan),
        _seal=_capability_seal(**facts),
        **facts,
    )
    _MINTED_CAPABILITIES.add(validated)
    return validated


_WORKSPACE_SPEC_RELATIVE = ".grip/workspace_spec.toml"
_RECEIPT_DIR_RELATIVE = ".grip/state/materialization"


def _read_canonical_workspace_spec_bytes(workspace_root: Path) -> bytes:
    """MaterializationPlan v1 invariant #2 applies to contract paths too, not only to
    operation paths: the canonical WorkspaceSpec must be reached
    through a symlink-free prefix and be a regular non-symlink file.

    Otherwise a symlink at .grip/workspace_spec.toml pointing outside the
    team root is accepted whenever its bytes happen to hash to the declared
    value -- the hash check confirms CONTENT, and says nothing about whether
    the file it read is inside the workspace at all."""
    spec_file = canonicalize_workspace_path(
        workspace_root, _WORKSPACE_SPEC_RELATIVE, field_name="workspace_spec_path"
    )
    if not spec_file.exists():
        raise MaterializationPlanError(
            "canonical WorkspaceSpec (.grip/workspace_spec.toml) not found -- "
            "workspace_spec_sha256 cannot be verified (MaterializationPlan v1 invariant #1)"
        )
    if not stat.S_ISREG(os.lstat(spec_file).st_mode):
        raise MaterializationPlanError(
            f"canonical WorkspaceSpec at {spec_file} is not a regular file "
            "(MaterializationPlan v1 invariant #2)"
        )
    return spec_file.read_bytes()


def _canonical_receipt_dir(workspace_root: Path) -> Path:
    """The receipt directory must be a real in-root directory reached
    through a symlink-free prefix: a symlinked
    .grip/state/materialization otherwise publishes the terminal receipt
    outside the team root entirely."""
    receipt_dir = canonicalize_workspace_path(
        workspace_root, _RECEIPT_DIR_RELATIVE, field_name="receipt_dir"
    )
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(receipt_dir).st_mode):
        raise MaterializationPlanError(
            f"receipt directory {receipt_dir} is not a real directory "
            "(MaterializationPlan v1 invariant #2)"
        )
    return receipt_dir


def compute_plan_hash(plan: dict[str, object]) -> str:
    """MaterializationPlan v1 invariant #9, the exact pinned canonical serialization: UTF-8
    JSON, keys sorted, no insignificant whitespace, non-ASCII unescaped.
    Default json.dumps separators and ensure_ascii=True produce different
    bytes and therefore a non-conformant hash. Public so callers and tests
    recompute it independently rather than trusting a receipt's own value.

    `default` is a TYPE adapter, not a change to the recipe: it fires only
    for objects json cannot serialize natively, so canonical bytes for a
    plain JSON graph are byte-identical either way. It exists so freezing a
    validated plan does not turn this public helper into a trap for the
    handlers that will hold one in B/C/D."""

    def _unfreeze(obj: object) -> object:
        if isinstance(obj, MappingProxyType):
            return dict(obj)
        raise TypeError(f"cannot canonicalize {type(obj).__name__} in a MaterializationPlan")

    return hashlib.sha256(
        json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_unfreeze
        ).encode("utf-8")
    ).hexdigest()


def materialization_receipt_path(workspace_root: Path, plan_id: str) -> Path:
    return workspace_root / ".grip" / "state" / "materialization" / f"{plan_id}.json"


def _screen_receipt_evidence(binding: _ConsumptionBinding) -> None:
    """The terminal receipt must be bound to the
    plan it claims to acknowledge, and its evidence must be as neutral as
    the plan.

    The plan's own operations are protected by a CLOSED schema whose
    per-kind allowlists permit no nested object carrier -- so recursive
    identity rejection there has little left to find. The result graph is
    the opposite: it is open, it is what gets persisted, and it was
    previously copied verbatim. That makes it the actual smuggling boundary,
    which is why the same rejection discipline is applied here rather than
    only upstream.

    Screens the CONSUMPTION BINDING, never the caller's object: the evidence
    was walked twice before -- once here, once by json serialization -- so a
    list whose __iter__ returned different contents per call screened clean
    and then persisted `secret` into the receipt anyway. Screening a value
    that is not the one persisted screens nothing."""
    op_results = binding.evidence
    if len(op_results) != len(binding.operation_kinds):
        raise MaterializationPlanError(
            f"receipt evidence has {len(op_results)} result(s) but the validated plan has "
            f"{len(binding.operation_kinds)} operation(s) -- a receipt cannot claim "
            "MATERIALIZED without evidence for every operation"
        )
    for idx, (result, expected_kind) in enumerate(zip(op_results, binding.operation_kinds)):
        if not isinstance(result, dict):
            raise MaterializationPlanError(
                f"receipt evidence[{idx}] must be an object, got {type(result).__name__}"
            )
        if result.get("kind") != expected_kind:
            raise MaterializationPlanError(
                f"receipt evidence[{idx}] is kind {result.get('kind')!r} but the validated plan's "
                f"operation {idx} is {expected_kind!r} -- evidence must correspond to its operation, "
                "in order"
            )
        _reject_identity_fields_recursive(result, path=f"receipt.operations[{idx}]")


def write_materialization_receipt(
    workspace_root: Path,
    validated: ValidatedPlan,
    op_results: list[dict[str, object]],
) -> Path:
    """Publish the terminal neutral receipt for a VALIDATED plan.

    Takes a ValidatedPlan capability rather than a raw dict: the
    writer previously accepted the live plan and an arbitrary result list,
    so a schema-invalid plan_id could escape the receipt directory and an
    unscreened result graph could be persisted verbatim. Holding the
    capability is the proof that the contract already ran.

    MaterializationPlan v1 invariant #10 -- publication order is exact and every step is
    load-bearing: same-directory temp -> write+flush -> fsync(temp file) ->
    atomic replace -> fsync(parent directory). Rename success alone is not
    durable acknowledgement; on power loss the rename can survive while the
    bytes do not. Callers performing destructive cleanup on the strength of
    a receipt must do it only after this returns.

    The temp file is created O_EXCL|O_NOFOLLOW: its name is
    predictable, so a plain open() would happily follow a pre-created
    symlink, overwrite whatever it points at, and then publish that symlink
    as the final receipt."""
    if not isinstance(validated, ValidatedPlan):
        raise MaterializationPlanError(
            "write_materialization_receipt requires a ValidatedPlan from "
            "validate_materialization_plan, not a raw plan"
        )
    # ONE verified consumption. Everything below reads `binding` and nothing
    # reads `validated` again -- verifying and then re-reading the live shell
    # is precisely the check/use window Atlas's callback drove through.
    binding = validated.consume(op_results)
    _screen_receipt_evidence(binding)

    receipt = {
        "plan_id": binding.plan_id,
        "unit_key": binding.unit_key,
        # Derived from the sealed snapshot at the consumption instant, NOT
        # recomputed from a graph that may have moved since.
        "plan_hash": binding.plan_hash,
        "schema_version": binding.schema_version,
        "workspace_spec_sha256": binding.workspace_spec_sha256,
        # §12.1 structural stage: MATERIALIZED is the terminal state OSS gr2
        # can honestly claim on its own.
        "stage": "MATERIALIZED",
        "applied_at": datetime.now(UTC).isoformat(),
        "operations": list(binding.evidence),
    }

    receipt_dir = _canonical_receipt_dir(workspace_root)
    # Both filenames come from the binding. The temp name carries the same
    # identity, so leaving it on a live read would only move the window
    # rather than close it.
    receipt_path = receipt_dir / f"{binding.plan_id}.json"
    tmp_path = receipt_dir / f"{binding.plan_id}.json.tmp-{os.getpid()}"

    payload = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        os.replace(tmp_path, receipt_path)
    except BaseException:
        # A failed replace leaves the temp behind otherwise -- found by this
        # closure's own residue test rather than reasoned about.
        tmp_path.unlink(missing_ok=True)
        raise

    # Durability is a FAILURE-PATH contract, not only an
    # ordering one. If the parent-directory fsync fails, the rename may not
    # survive a crash -- yet the receipt is already visible at its published
    # path, so a caller that treats "the writer returned" or "a receipt
    # exists" as durable acknowledgement would proceed to destructive
    # cleanup on the strength of a receipt that could vanish. A publication
    # that cannot be made durable must not remain published.
    try:
        dir_fd = os.open(receipt_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        receipt_path.unlink(missing_ok=True)
        raise
    return receipt_path

# gr2 Core-Engine Seam

Status: D4 seam contract for `grip#753`.

This document freezes the Rust gr2 prototype as reference material and names
the engine API the Python implementation must present. It does not authorize a
Rust core build. Python remains the product and CLI surface until measured
performance evidence says otherwise.

## Decision

### Native workspace inclusion

A new native workspace commits `.gitinclude` as its inclusion declaration.
The existing inclusion compiler generates `.gitignore`, with default exclusion
and an explicit final rule excluding `.gitignore` itself. Including that
generated file is refused. Init and store commit regenerate the file from the
working declaration. Selected-commit checkout regenerates it from the selected
commit's declaration before materializing members.

Store commit checks both the proposed document/member paths and existing index
entries against the declaration's canonical policy, independently of nested
ignore negations. Canonical exclusions use the compiler's conservative
casefold policy, so case aliases cannot undo an exclusion. An excluded indexed path is refused without
clearing the index. Ignore rules alone cannot constrain forced staging or Git
gitlink insertion. Inside an included directory, child `.gitignore` files retain
normal Git exclusions and may themselves be tracked. Those exclusions prevent
new ignored files being staged, while already-tracked files retain normal Git
behavior. Generated root output refuses symlink/nonregular or tracked-owner
conflicts before replacement. Publication replaces the file atomically without
opening the old output for truncation. Existing adopted roots without `.gitinclude` retain their
owner's ignore file and tracked history. This change does not migrate or untrack
those files automatically.

`gr2` has one live implementation surface:

- Python owns CLI, user workflow, JSON/human rendering, error presentation,
  event emission, hooks, lane UX, and packaging.
- The dormant Rust gr2 prototype is reference material for convergence
  algorithms, not a second live binary or CI surface.
- A future Rust core may implement the same engine API behind an adapter, but
  the CLI must not be rewritten around it.

The seam is the `gr2 core-engine API`. It sits below `gr2/gr2/python_cli/app.py` and
above raw filesystem/git operations.

```text
Typer CLI / product UX
    -> core-engine adapter
    -> Python engine implementation today
    -> optional Rust engine implementation later
    -> filesystem + git
```

## Frozen Rust Reference

The Rust prototype is useful as an algorithm reference for:

- `gr2/src/plan.rs`: `spec -> plan -> apply` convergence, guarded mutation,
  and apply-state recording.
- `gr2/src/lane.rs`: lane record shape, lane creation validation, and durable
  lane metadata.
- `gr2/src/repo_status.rs`: repo status classification and ahead/behind
  inspection.

It is not the owner of the CLI. D4 implementation should make that visible by
removing it from live Cargo workspace membership, deleting the duplicate root
`gr2` bin, and moving or clearly marking the Rust code as reference-only.

## API

The core engine presents structured functions. They return typed data or
plain structured dictionaries. They do not print, parse CLI args, call Typer,
exit the process, or read state it does not own.

### `load_spec`

```python
load_spec(workspace_root: Path) -> WorkspaceSpec
```

Responsibilities:

- read `.grip/workspace_spec.toml`
- validate workspace, repo, and unit shape
- preserve opaque fields such as `agent_id` without interpreting identity
- report validation issues as structured data

Non-responsibilities:

- resolving org membership
- deriving `owner_unit` from `agent_id`
- consulting entitlements

Current Python references:

- `gr2/gr2/python_cli/spec_apply.py::load_workspace_spec_doc`
- `gr2/gr2/python_cli/spec_apply.py::validate_spec`

### `plan`

```python
plan(spec: WorkspaceSpec, fs_state: WorkspaceState) -> ExecutionPlan
```

Responsibilities:

- compare declared workspace intent with current filesystem/git state
- produce ordered operations
- classify blockers separately from executable work
- remain dry-run safe

Non-responsibilities:

- mutating repos
- rendering terminal output
- running lifecycle hooks

Current Python references:

- `gr2/gr2/python_cli/spec_apply.py::build_plan`
- `gr2/gr2/python_cli/syncops.py::build_sync_plan`

### `apply`

```python
apply(plan: ExecutionPlan, guards: ApplyGuards) -> AppliedState
```

Responsibilities:

- execute approved operations
- preserve dirty-work guards
- stop on blocking failure and report partial state explicitly
- record local apply state

Non-responsibilities:

- deciding policy
- hiding partial failure behind a success response
- silently pulling, rebasing, or discarding work without an explicit guard

Current Python references:

- `gr2/gr2/python_cli/spec_apply.py::apply_plan`
- `gr2/gr2/python_cli/syncops.py::run_sync`

### `repo_status`

```python
repo_status(targets: list[RepoTarget], policy: RepoPolicy) -> RepoStatusReport
```

Responsibilities:

- inspect repo existence and git state
- classify dirty, ahead, behind, detached, missing, and path-conflict states
- return actionable status rows without mutation

Non-responsibilities:

- opening PRs
- selecting reviewers
- mutating branch state

Current Python references:

- `gr2/gr2/prototypes/repo_maintenance_prototype.py`
- `gr2/gr2/python_cli/app.py::repo_status`

### `materialize`

```python
materialize(request: MaterializeRequest) -> MaterializeResult
```

Responsibilities:

- create or refresh workspace repo checkouts
- create lane-local repo checkouts from declared lane metadata
- use cache/reference clone optimization without changing the user model
- report first-materialization vs already-present state

Non-responsibilities:

- reading lane envelopes it does not own
- resolving whether a caller is allowed to create the lane
- deriving identity from branch, path, or handle strings

Current Python references:

- `gr2/gr2/python_cli/gitops.py::clone_repo`
- `gr2/gr2/python_cli/gitops.py::ensure_lane_checkout`
- `gr2/gr2/python_cli/app.py::_materialize_lane_repos`

### Lane checkout coordinates

`lane.toml` and lane leases remain local metadata under
`.grip/state/lanes/<unit>/<lane>`. New materialized lanes record the optional
workspace-relative `checkout_root = "agents/<unit>/lanes/<lane>"`. A native lane
root is an ordinary Git checkout. Its current HEAD's committed `grip.toml` and
gitlinks determine the complete member set, declared child paths and pins.
Member keys are not child paths. Unit launch homes remain
`agents/<unit>/home`, outside `.grip`.

The owning lane resolver uses recorded coordinates, never directory existence:

- Bound lanes retain their explicit `bound_worktree`. They cannot also declare
  `checkout_root`.
- Materialized lanes with `checkout_root` use that validated workspace-relative
  root. Native child paths come from its current committed workspace document.
  Spec-backed legacy lanes retain `<checkout_root>/repos/<repo>`.
- Existing project reviews record their owning `reviews/<unit>/<lane>` root in
  the same field. `review-ephemeral` retains its read-only kind and does not use
  the native workspace member projection. This does not move review directories.
- Older materialized documents without the field retain the deterministic
  `.grip/state/lanes/<unit>/<lane>/repos/<repo>` location.

Creating a lane does not relocate or rebind existing lanes. The existing cache
seeder and ordinary reference-clone materializer provide independent mutable Git
state, allowing two units to use the same branch. Failed creation removes only
artifacts created by that attempt. A complete recorded fork base retains a usable
checkout after a lifecycle-hook refusal. Cleanup failure preserves the original
error and reports the secondary failure.

Native lane creation defaults to the source workspace HEAD. `--workspace-commit`
selects another resolvable commit before materialization. The selected committed
document wins over an ambient spec. Enter follows the lane root's current HEAD,
without a separate remembered membership or path map. Removing a member from a
workspace commit does not authorize deleting its previous checkout.

After root detach, cache, clone or member-checkout failures report PART-APPLIED
with the selected commit, failing member/stage and completed members. Existing
checkouts and usable partial materialization remain in place. Diagnostic storage
or output failures do not replace the original exception.

## Boundary Rules

1. The engine does not own identity.

   `owner_unit` and `agent_id` are opaque caller-supplied keys. The engine may
   store and echo them, but it must not derive, parse, or authorize from them.

2. The engine does not own lane envelopes.

   gr2 lane definitions and leases live under
   `.grip/state/lanes/<owner_unit>/<lane>`. Physical checkouts are resolved from
   the owning lane document's `checkout_root`, explicit bound coordinate, or
   deterministic legacy coordinate described above. Existing project reviews
   retain their owning review location. An unrelated external lane envelope
   does not become read authority merely by existing in the workspace.

3. The engine does not own CLI shape.

   CLI commands adapt arguments into engine requests and render engine results.
   Engine code returns structured state and raises typed or structured errors.

4. The engine does not own hooks policy.

   gr2 exposes a neutral hook seam and nothing more. It does not register
   hooks, decide hook policy, or define hook behaviour.

5. The engine does not own transport.

   Git is the current transport. A Rust core may optimize git inspection or
   convergence. gr2 does not route between agents and does not bridge to chat
   or memory systems.

## Adapter Shape

Python should route engine calls through one module before the CLI invokes
them. The adapter is the only place that chooses implementation.

```python
class CoreEngine:
    def load_spec(self, workspace_root: Path) -> WorkspaceSpec: ...
    def plan(self, spec: WorkspaceSpec, fs_state: WorkspaceState) -> ExecutionPlan: ...
    def apply(self, plan: ExecutionPlan, guards: ApplyGuards) -> AppliedState: ...
    def repo_status(self, targets: list[RepoTarget], policy: RepoPolicy) -> RepoStatusReport: ...
    def materialize(self, request: MaterializeRequest) -> MaterializeResult: ...
```

Initial implementation:

- `PythonCoreEngine`, calling existing Python modules.

Future optional implementation:

- `RustCoreEngine`, using a subprocess or FFI boundary that implements the same
  request/response schema.

The CLI must not know which implementation ran.

## Rust-Core Build Gate

Do not build the Rust core until measured evidence requires it. Examples of
acceptable evidence:

- full-gripspace `plan` or `repo_status` exceeds an agreed interactive target
- lane materialization spends meaningful time in Python-bound graph or diff
  work rather than git subprocesses
- repeated profiling shows Python object traversal, not git or filesystem IO,
  is the bottleneck

Forward-looking performance anxiety is not evidence. Until the gate trips,
the Rust prototype remains frozen reference material and Python remains the
living spec.

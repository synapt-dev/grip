# gr2 Core-Engine Seam

Status: D4 seam contract for `grip#753`.

This document freezes the Rust gr2 prototype as reference material and names
the engine API the Python implementation must present. It does not authorize a
Rust core build. Python remains the product and CLI surface until measured
performance evidence says otherwise.

## Decision

`gr2` has one live implementation surface:

- Python owns CLI, user workflow, JSON/human rendering, error presentation,
  event emission, hooks, lane UX, and packaging.
- The dormant Rust gr2 prototype is reference material for convergence
  algorithms, not a second live binary or CI surface.
- A future Rust core may implement the same engine API behind an adapter, but
  the CLI must not be rewritten around it.

The seam is the `gr2 core-engine API`. It sits below `python_cli/app.py` and
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
exit the process, or read premium-only state.

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
- consulting premium entitlements

Current Python references:

- `python_cli/spec_apply.py::load_workspace_spec_doc`
- `python_cli/spec_apply.py::validate_spec`

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

- `python_cli/spec_apply.py::build_plan`
- `python_cli/syncops.py::build_sync_plan`

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

- deciding premium policy
- hiding partial failure behind a success response
- silently pulling, rebasing, or discarding work without an explicit guard

Current Python references:

- `python_cli/spec_apply.py::apply_plan`
- `python_cli/syncops.py::run_sync`

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

- `prototypes/repo_maintenance_prototype.py`
- `python_cli/app.py::repo_status`

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

- reading premium lane envelopes under `.grip/lanes`
- resolving whether a caller is allowed to create the lane
- deriving identity from branch, path, or handle strings

Current Python references:

- `python_cli/gitops.py::clone_repo`
- `python_cli/gitops.py::ensure_lane_checkout`
- `python_cli/app.py::_materialize_lane_repos`

## Boundary Rules

1. The engine does not own identity.

   `owner_unit` and `agent_id` are opaque caller-supplied keys. The engine may
   store and echo them, but it must not derive, parse, or authorize from them.

2. The engine does not own premium envelopes.

   OSS gr2 lane records live under `agents/<owner_unit>/lanes/<lane>`.
   Premium lane envelopes may live under `.grip/lanes`, but OSS gr2 must not
   read that namespace.

3. The engine does not own CLI shape.

   CLI commands adapt arguments into engine requests and render engine results.
   Engine code returns structured state and raises typed or structured errors.

4. The engine does not own hooks policy.

   The OSS UX layer exposes only a neutral hook seam; hook registration,
   policy, and behavior live in premium-side plugins.

5. The engine does not own transport.

   Git is the current transport. A Rust core may optimize git inspection or
   convergence, but cross-agent routing and channel/recall bridges stay above
   the engine seam.

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

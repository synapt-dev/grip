# gr2 Hook-Based Repo Config

This document defines the hook-based configuration model for Python-first
`gr2`.

The design goal is:

- keep the workspace spec bare
- let each repo carry its own materialization and lifecycle behavior
- make Python `gr2` the first production UX surface
- preserve a clean migration path from `gr1` to Python `gr2` to Rust `gr2`

The important boundary is:

- workspace config says **what exists**
- repo hook config says **how this repo behaves when materialized**

## 1. Why This Model

The current `gr` manifest mixes several concerns:

- repo registry
- workspace-level link/copy behavior
- workspace hooks
- agent/runtime configuration

That made the workspace manifest too heavy and too central. It also means repo
behavior is hard to move with the repo.

The hook-based model changes that:

- `WorkspaceSpec` stays narrow and stable
- repo-local `.gr2/hooks.toml` travels with the repo
- materialization becomes a small orchestrator that reads repo contracts
- Python `gr2` can prove the UX before we freeze implementation in Rust

## 2. Core Model

### 2.1 Bare WorkspaceSpec

The workspace file only declares:

- workspace identity
- repos
- units
- optional workspace-wide constraints compiled from premium

Example:

```toml
workspace_name = "synapt-codex"

[[repos]]
name = "grip"
path = "repos/grip"
url = "git@github.com:synapt-dev/grip.git"

[[repos]]
name = "synapt"
path = "repos/synapt"
url = "git@github.com:synapt-dev/synapt.git"

[[repos]]
name = "synapt-private"
path = "repos/synapt-private"
url = "git@github.com:synapt-dev/synapt-private.git"

[[units]]
name = "atlas"
path = "agents/atlas"
agent_id = "atlas-agent"
repos = ["grip", "synapt", "synapt-private"]

[[units]]
name = "apollo"
path = "agents/apollo"
agent_id = "apollo-agent"
repos = ["grip", "synapt", "synapt-private"]

[workspace_constraints]
max_concurrent_edit_leases_global = 2

[workspace_constraints.required_reviewers]
grip = 1
synapt = 1
synapt-private = 2
```

Rules:

- no linkfile/copyfile definitions here
- no repo-local lifecycle commands here
- no org logic here
- compiled workspace-wide constraints are allowed here

### 2.2 Repo-Local Hook File

Each repo may provide:

- `.gr2/hooks.toml`

This file defines repo-local:

- file projections
- lifecycle hooks
- repo policies
- optional tool/runtime defaults

If the file is absent, the repo is treated as having no special behavior.

## 3. Hook Schema

The starting schema should be small and explicit.

```toml
version = 1

[repo]
name = "synapt"

[[files.link]]
src = "CLAUDE.md"
dest = "{workspace_root}/CLAUDE.md"
if_exists = "error"

[[files.copy]]
src = ".env.example"
dest = "{unit_root}/repos/synapt/.env.example"
if_exists = "error"

[[lifecycle.on_materialize]]
name = "editable-install"
command = "uv pip install -e ."
cwd = "{repo_root}"
when = "first_materialize"
on_failure = "block"

[[lifecycle.on_enter]]
name = "show-dev-hints"
command = "python scripts/dev_hints.py"
cwd = "{repo_root}"
when = "always"
on_failure = "warn"

[[lifecycle.on_exit]]
name = "cleanup-temp-state"
command = "python scripts/cleanup.py"
cwd = "{repo_root}"
when = "dirty"
on_failure = "warn"

[policy]
required_reviewers = 1
allow_lane_kinds = ["feature", "review"]
preferred_exec = ["pytest -q"]
```

### 3.1 Supported Sections

Initial sections:

- `[repo]`
- `[[files.link]]`
- `[[files.copy]]`
- `[[lifecycle.on_materialize]]`
- `[[lifecycle.on_enter]]`
- `[[lifecycle.on_exit]]`
- `[policy]`

Possible later sections:

- `[exec]`
- `[tooling]`
- `[[lifecycle.on_review_start]]`
- `[[lifecycle.on_review_complete]]`

### 3.2 File Projection Conflict Policy

Each file projection must define how conflicts are handled with:

- `if_exists = "skip" | "overwrite" | "merge" | "error"`

Default:

- `error`

Meaning:

- `skip`
  - leave the existing destination untouched
- `overwrite`
  - replace the destination with the source
- `merge`
  - delegate to a merge-capable projection handler for supported file types
- `error`
  - fail materialization instead of silently colliding

This matters immediately because multiple repos may want to project to the
same workspace path, for example `CLAUDE.md`.

### 3.3 Lifecycle `when` Semantics

The initial `when` values are:

- `first_materialize`
  - run only when the repo is being materialized into this workspace target for
    the first time
- `always`
  - run every time the lifecycle stage is reached
- `dirty`
  - run only when the repo has uncommitted local state, including tracked
    modifications, staged changes, or untracked files
- `manual`
  - never run automatically; only run when the user explicitly requests the
    hook or hook group

### 3.4 Hook Failure Policy

Each lifecycle hook may define:

- `on_failure = "block" | "warn" | "skip"`

Default behavior:

- `on_materialize`
  - `block`
- `on_enter`
  - `warn`
- `on_exit`
  - `warn`
- file projections
  - `block`

Meaning:

- `block`
  - stop the current operation with a failure
- `warn`
  - record the failure and continue
- `skip`
  - do not treat failure as an error and continue silently except for logging

These defaults are deliberate:

- broken repo setup during materialization should stop early
- broken enter/exit hooks should not trap users outside their lane
- broken file projections should not fail silently

### 3.5 Template Variables

Allowed interpolation variables:

- `{workspace_root}`
- `{unit_root}`
- `{lane_root}`
- `{repo_root}`
- `{repo_name}`
- `{lane_owner}`
- `{lane_subject}`
- `{lane_name}`

Rules:

- interpolation is explicit, not shell-magic
- undefined variables are validation errors
- paths resolve before command execution

## 4. Materialization Flow

`gr2 apply` should use the following flow:

1. read `WorkspaceSpec`
2. materialize shared cache / repo source if configured
3. materialize unit-local or lane-local working checkouts
4. for each materialized repo root, read `.gr2/hooks.toml` if present
5. apply file projections
6. run `on_materialize` hooks
7. write local state/logs

`gr2 lane enter` should:

1. resolve current lane root
2. for each repo in scope, read `.gr2/hooks.toml`
3. run `on_enter` hooks
4. emit lane-enter event

`gr2 lane exit` should:

1. run `on_exit` hooks for repos in scope
2. emit lane-exit event

Important rule:

- hook config is consumed by the workspace orchestrator
- repo code never has to know about units, lanes, or org logic beyond the
  interpolated local paths and lane names it is given

## 5. Example Repo Hooks

These are grounded in the actual repos we have.

### 5.1 `grip`

`grip` is the workspace router. Its repo-local hooks should stay light.

Example `.gr2/hooks.toml`:

```toml
version = 1

[repo]
name = "grip"

[policy]
required_reviewers = 1
allow_lane_kinds = ["feature", "review", "scratch"]
preferred_exec = ["cargo test --quiet"]

[[lifecycle.on_materialize]]
name = "cargo-check"
command = "cargo check -q"
cwd = "{repo_root}"
when = "manual"
on_failure = "warn"
```

Why:

- `grip` should not auto-run expensive hooks on every enter
- repo policy can still declare reviewer count and preferred test surface

### 5.2 `synapt`

`synapt` is Python and often needs an editable install in the active
workspace.

Example:

```toml
version = 1

[repo]
name = "synapt"

[policy]
required_reviewers = 1
allow_lane_kinds = ["feature", "review", "scratch"]
preferred_exec = ["pytest tests/ -q"]

[[lifecycle.on_materialize]]
name = "editable-install"
command = "uv pip install -e ."
cwd = "{repo_root}"
when = "first_materialize"
on_failure = "block"

[[lifecycle.on_enter]]
name = "workspace-doctor"
command = "python -m synapt.doctor --workspace {workspace_root}"
cwd = "{repo_root}"
when = "manual"
```

Why:

- this repo frequently suffers from stale editable install drift
- making that behavior repo-local is better than hiding it in a workspace-level
  manifest

### 5.3 `synapt-private`

`synapt-private` already carries private config and has stronger review needs.

Example:

```toml
version = 1

[repo]
name = "synapt-private"

[policy]
required_reviewers = 2
allow_lane_kinds = ["feature", "review"]
preferred_exec = ["pytest tests/ -q"]

[[files.link]]
src = "config/models.json"
dest = "{lane_root}/repos/synapt-private/.gr2-linked/models.json"
if_exists = "error"

[[lifecycle.on_materialize]]
name = "editable-install"
command = "uv pip install -e ."
cwd = "{repo_root}"
when = "first_materialize"
on_failure = "block"

[[lifecycle.on_enter]]
name = "validate-private-config"
command = "python scripts/validate_config.py"
cwd = "{repo_root}"
when = "manual"
```

Why:

- stronger reviewer requirements belong in repo policy
- private config validation should travel with the repo
- the workspace should not need to know what model files matter here

## 6. What Materialization Actually Does

For a lane touching `grip`, `synapt`, and `synapt-private`, `gr2` should:

1. create the lane root
2. materialize the lane-local or unit-local checkouts
3. load `.gr2/hooks.toml` from each checkout in `[[repos]]` declaration order
4. apply file actions declared by each repo
5. run `on_materialize` for first-time repo setup
6. record what ran in `.grip/state/`

That means:

- the workspace orchestrator remains generic
- repo-specific behavior travels with the repo
- the repo author owns the repo’s materialization contract

## 7. Workspace vs Repo Responsibility

### WorkspaceSpec owns

- repo list
- unit list
- lane ownership model
- workspace-wide constraints
- materialization topology

### Repo hook config owns

- repo-local file projections
- repo-local lifecycle behavior
- repo-local policy defaults
- repo-local preferred commands

### Premium owns

- durable identity
- org roles
- entitlements
- compilation of org/policy into workspace constraints

## 8. Migration Path

We need one migration story, not three separate products.

### 8.1 `gr1 -> Python gr2`

Phase 1:

- keep `gr1` alive
- introduce Python `gr2` alongside it
- use Python `gr2` for lane, lease, and materialization UX
- continue reading existing repo state during transition

Goal:

- UX migration first
- not backend migration first

### 8.2 Python gr2 -> Rust gr2

The Rust port should be a backend swap, not a user-facing redesign.

That means Python `gr2` must already define:

- CLI nouns
- config schema
- event formats
- lane semantics
- hook semantics

Rust then reimplements:

- parsing
- execution
- performance-sensitive paths

But keeps:

- command names
- config shapes
- event schema
- lane semantics

### 8.3 Compatibility Rule

If moving from Python `gr2` to Rust `gr2` requires users to relearn the model,
the migration failed.

## 8.4 `agents.toml` Relationship

Current `agents.toml` should be treated as input to the compilation step, not
as a parallel runtime authority once Python `gr2` is active.

Recommended direction:

- `agents.toml` remains a premium/control-plane input during transition
- compilation resolves it into:
  - workspace `units`
  - `agent_id`
  - repo access
  - lane limits
- `WorkspaceSpec` becomes the OSS runtime contract

## 9. Python-First CLI Implication

The Python CLI should present the same nouns we intend to keep:

- `gr2 repo status`
- `gr2 lane create`
- `gr2 lane enter`
- `gr2 lane lease acquire`
- `gr2 review requirements`
- `gr2 apply`

It should be a real CLI, not just prototype scripts.

The point is:

- validate the UX with real use
- identify hot paths
- only then move those paths to Rust

## 10. Open Questions

These still need prototype pressure:

1. Should file actions run against unit-local roots, lane-local roots, or both?
2. Which lifecycle hooks are safe by default versus manual-only?
3. How do hook failures interact with `apply`?
   - block
   - warn
   - retry
4. Do we want per-repo review requirements only, or repo+path granularity later?
5. How do repo hooks compose with shared cache-backed materialization?

## 11. Recommended Next Step

Prototype this model in Python before touching Rust:

1. add `.gr2/hooks.toml` parser
2. add a minimal Python `gr2` CLI surface
3. implement:
   - file projection
   - `on_materialize`
   - `on_enter`
   - `on_exit`
4. validate with:
   - `grip`
   - `synapt`
   - `synapt-private`

The success condition is simple:

- the workspace spec gets smaller
- repo behavior becomes more portable
- the team can use Python `gr2` daily without needing a second UX migration

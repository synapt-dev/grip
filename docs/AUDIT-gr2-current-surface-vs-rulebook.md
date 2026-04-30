# Audit: Current gr2 Surface vs Rulebook

## Scope

This audit compares the current `gr2` implementation surface against:

- `docs/PLAN-gr2-needs-and-criteria.md`
- `docs/PLAN-gr2-repo-maintenance-and-lanes.md`

Implementation reviewed:

- `gr2/src/args.rs`
- `gr2/src/dispatch.rs`
- `gr2/src/plan.rs`
- `gr2/src/spec.rs`

## Executive Summary

Current `gr2` is a useful structural bootstrap, not yet a first-class workspace.

What it does reasonably well:

- initialize a team-workspace skeleton
- register repos and units
- emit a basic workspace spec
- plan and apply structural convergence
- keep some repo mutation out of plain `apply`

What it does not yet do:

- named lanes
- shared/private context
- lane-aware execution
- explicit repo-maintenance surfaces
- review-lane isolation
- durable recovery/state strong enough for multi-agent use

Bottom line:

- as a structural foundation: promising
- as a human + multi-agent operating surface: incomplete

## Criteria Assessment

### 1. Structural and Repo-State Separation

Status: `Partial`

Strength:

- the current model does separate `plan/apply` from most explicit branch/sync behavior

Weakness:

- `materialize_unit()` clones repos directly during `apply`, which is acceptable as structural convergence
- but there is still no explicit repo-maintenance read/write surface above it
- `Apply --autostash` exists, but the user still lacks a separate repo-status model to understand when it would matter

Assessment:

The safety boundary is directionally right, but incomplete because repo-state
inspection is still missing.

### 2. Named Multi-Repo Lanes

Status: `Fail`

Current surface:

- no lane commands
- no lane metadata
- no lane types
- no lane-local branch/PR/context model

Assessment:

This is the largest missing capability relative to the rulebook.

### 3. Cheap Parallelism

Status: `Partial`

Strength:

- workspace topology is moving toward clone-backed isolation rather than shared worktrees

Weakness:

- cache exists only as a path in spec; there is no active lane/cache model
- no lane creation surface
- no disposable review lane concept

Assessment:

The intended direction is there, but the system cannot yet prove cheap
parallelism in practice.

### 4. Shared and Private Context

Status: `Fail`

Current surface:

- `config/` exists at init time
- no context model in spec or metadata
- no unit-private context roots
- no lane-local context inheritance

Assessment:

This is entirely absent from current `gr2`.

### 5. Lane-Aware Execution

Status: `Fail`

Current surface:

- no `exec` surface
- no lane scoping
- no repo-subset execution model

Assessment:

The current implementation offers no first-class answer for build/test/run.

### 6. Dirty Work Preservation

Status: `Partial`

Strength:

- current branch includes explicit dirty repo detection inside unit repo checkouts
- autostash/preservation hooks exist in `plan.rs`

Weakness:

- preservation is apply-time only
- no separate repo-maintenance command family
- no user-facing recovery/status surface yet

Assessment:

This is meaningful progress, but still not a complete preservation story.

### 7. PR and Review Isolation

Status: `Fail`

Current surface:

- no `checkout-pr`
- no review lanes
- no durable PR associations

Assessment:

Review remains external to the workspace model.

### 8. Cross-Repo Feature Coherence

Status: `Fail`

Current surface:

- units can list repos
- no branch map
- no PR grouping
- no expected verification set

Assessment:

The workspace knows repo membership, but not feature coherence.

### 9. Deterministic Status Surfaces

Status: `Partial`

Strength:

- `plan` is deterministic for structural operations
- `spec show/validate` exist

Weakness:

- no JSON/status model for repo state
- no lane status
- no execution status
- no PR-linkage status

Assessment:

Structural visibility exists. Operational visibility does not.

### 10. Multi-Repo Scratchpads

Status: `Fail`

Current surface:

- only one implicit working context per unit path
- no multiple scratchpads
- no review or repro lanes

Assessment:

Current `gr2` cannot yet support the core workflow-switching use case.

## Surface-Specific Findings

### `args.rs`

What exists:

- `init`
- `doctor`
- `team`
- `repo`
- `unit`
- `spec`
- `plan`
- `apply`

Finding:

The command surface is still structural/registry-heavy. There is no runtime
surface for:

- repo maintenance
- lane management
- execution
- context

### `dispatch.rs`

Finding 1:

`TeamCommands` and `UnitCommands` both materialize directories under `agents/`,
but they represent different concepts (`agent.toml` vs `unit.toml`) without a
clear higher-level model.

Why this matters:

- the rulebook needs unit home, lanes, context, and execution roots
- the current split is likely to confuse future implementation if not unified

Finding 2:

`RepoCommands::Add` creates metadata directories immediately under `repos/`.
That is acceptable for registration, but it mixes registry behavior with
materialized path creation before a stronger lane/cache model exists.

### `spec.rs`

Finding 1:

`WorkspaceSpec v1` is intentionally narrow, which is good.

Finding 2:

`WorkspaceSpec::from_workspace()` does not recover the richer intent needed for
future lanes:

- no branch map
- no lane state
- no context roots
- no execution defaults

Finding 3:

`read_registered_units()` reconstructs units from directory presence and
currently initializes `repos` as empty.

Why this matters:

- round-tripping from the filesystem loses intent
- that is acceptable for bootstrap, but not for a durable lane workspace

### `plan.rs`

Finding 1:

The current branch improved real gaps:

- partially materialized unit repos are now detected
- link planning exists
- dirty nested repos are detected

Finding 2:

`materialize_unit()` clones repos directly into the unit path.

This is acceptable for now, but it bypasses the future cache/lane model and
will need refactoring once lanes become first-class.

Finding 3:

Dirty repo detection only considers repos inside units with planned operations.

That is a reasonable apply guard, but it is not the same as a general
repo-maintenance status surface.

## Practical Gaps to Fix First

### P0

- `gr2 repo status`
- lane metadata format

These are the minimum requirements to make repo-state and lane-state legible.

### P1

- lane create/status/enter/remove
- lane-aware execution

These make the workspace usable for actual task switching.

### P2

- `lane checkout-pr`
- explicit repo sync/pull/fetch surfaces

These complete the review and maintenance story.

## Conclusion

Current `gr2` passes as a structural foundation and fails as a complete
human/agent workspace.

That is not a criticism of the implementation direction. It is the correct
reading of where the surface is today.

The good news is that the missing pieces are now sharply defined:

- visibility
- lane metadata
- lane-aware execution
- isolated review/scratch workflows

Those are exactly the right next layers to build.

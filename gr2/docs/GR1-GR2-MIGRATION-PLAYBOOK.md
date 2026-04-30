# gr1 -> gr2 Migration Playbook

Date: 2026-04-17  
Issue: `grip#522`  
Premium boundary: `grip` is OSS because this document covers workspace migration mechanics, feature compatibility, and cutover sequencing for `gr1` and `gr2`. Identity binding, org routing, and agent identity migration are premium concerns and are explicitly out of scope here.

## Thesis

`gr1 -> gr2` migration should be treated as a staged workspace cutover, not a flag day rewrite. `gr2` replaces `gr1` only when the workspace can be materialized, synchronized, executed, and reviewed through `gr2` without depending on hidden `gr1` behavior. Identity and org-aware behavior are not part of the OSS cutover. They must attach through a premium plugin seam after the workspace migration is complete.

## Scope

This playbook covers:

- detecting a `gr1` workspace
- compiling a parallel `gr2` workspace spec
- validating repo, lane, and execution behavior in a dual-run window
- cutover criteria for day-to-day use
- the deprecation path for `gr1`

This playbook does **not** cover:

- agent identity migration
- org routing
- persistent identity binding
- premium policy compilation

Those concerns belong to premium. The OSS playbook may reference the premium seam, but it must not implement identity resolution.

## Definitions

- **gr1 workspace**: `.gitgrip/` manifest, state files, and existing day-to-day `gr` workflow
- **gr2 workspace**: `.grip/workspace_spec.toml`, lane roots, hook-based repo behavior, and Python-first `gr2` commands
- **dual-run window**: period where `gr1` remains the production fallback while `gr2` is validated against the same repos
- **cutover**: point where daily workspace operations move to `gr2`

## Migration Principles

1. Build `gr2` in parallel. Do not destroy the `gr1` workspace first.
2. Preserve existing `gr1` state as migration snapshots.
3. Validate with real repos, not metadata-only mocks.
4. Cut over workspace mechanics first. Identity migration happens later through premium.
5. Deprecate only after `gr2` proves equivalence on the required workflows.

## Compatibility Matrix

| Capability | gr1 | gr2 (current target) | Migration note |
|---|---|---|---|
| Detect existing workspace | Yes | Yes | `gr2 workspace detect-gr1` is the entrypoint |
| Parallel workspace spec generation | N/A | Yes | `gr2 workspace migrate-gr1` writes `.grip/workspace_spec.toml` alongside `.gitgrip/` |
| Preserve prior state | Yes | Yes | snapshots stored under `.grip/migrations/gr1/` |
| Workspace materialization | Yes | Yes | must be validated against real git repos |
| Repo hook execution | Limited | Yes | `.gr2/hooks.toml` becomes the repo-local source of truth |
| Lane create / enter / exit / lease | No first-class lane model | Yes | migration does not invent lane state from `gr1` |
| Exec in lane context | Partial / ad hoc | Targeted | `gr2 exec` must be validated before cutover |
| Review lane checkout | Existing PR workflows in `gr` | Targeted | required for day-to-day review parity |
| Sync / convergence | Yes | In progress | real-git sync validation is a cutover gate |
| Identity-aware spawn / recall binding | Mixed legacy behavior | Out of scope in OSS | premium plugin handles identity binding after workspace cutover |

## Migration Stages

### Stage 0: Preconditions

Before starting a workspace migration:

- `gr1` workspace is healthy
- repos are synced
- there is no unreviewed destructive workspace change in flight
- premium-only identity behavior is not required to complete the OSS migration

Recommended operator checks:

1. verify current `gr` status is clean enough to reason about
2. identify the workspace owner and migration window
3. capture current `gr1` layout and expected repo set

### Stage 1: Detect and Snapshot

Run:

```bash
gr2 workspace detect-gr1 <workspace_root>
gr2 workspace migrate-gr1 <workspace_root>
```

Expected outputs:

- `.grip/workspace_spec.toml`
- `.grip/migrations/gr1/` snapshots of:
  - `state.json`
  - `sync-state.json`
  - `griptrees.json`
  - `gripspace.yml`

Required rule:

- `.gitgrip/` remains untouched during initial migration

### Stage 2: Validate the Compiled Workspace

Before materializing anything operationally:

1. review the generated `workspace_spec.toml`
2. confirm repo names, paths, and URLs are correct
3. confirm writable vs reference repo classification is correct
4. remove any premium identity leakage from the spec

Current design rule:

- units are workspace-local ownership buckets only
- `gr2` must not compile or infer durable agent identity
- if identity mapping is needed, premium must supply it separately

### Stage 3: Materialize in Parallel

Materialize the `gr2` workspace without deleting the `gr1` layout:

```bash
gr2 workspace materialize <workspace_root>
gr2 spec validate <workspace_root>
gr2 plan <workspace_root>
gr2 apply <workspace_root>
```

Acceptance at this stage:

- repos clone or resolve correctly
- shared repo cache seeds correctly
- `.gr2/hooks.toml` loads cleanly
- file projections and `on_materialize` hooks behave as expected
- no existing `gr1` state is damaged

### Stage 4: Real-Git Validation

This stage is required before cutover.

Validate against real repos:

- branch creation
- lane-local checkout isolation
- review checkout
- dirty-state behavior
- apply convergence
- sync behavior
- lane-aware exec

This is where `grip#555` matters. Migration design without real-git validation is not credible enough for cutover.

### Stage 5: Dual-Run Window

Operate with:

- `gr1` as fallback
- `gr2` as the proving path

During dual-run:

- compare outcomes on the same workspace operations
- record mismatch cases
- treat missing parity as cutover blockers

Required workflows to validate in dual-run:

1. workspace materialize / sync
2. lane create / enter / exit
3. lane-aware exec
4. review checkout
5. apply convergence

### Stage 6: Cutover Decision

Cut over only when the criteria below are satisfied.

## Cutover Criteria

`gr2` is ready to become the day-to-day workspace surface only when all of these are true:

1. `gr2` MVP definition is approved (`grip#582`)
2. real-git validation passes (`grip#555`)
3. lane-aware exec is working (`grip#544`)
4. migration smoke suite passes in Sentinel QA
5. apply/sync behavior is stable enough for the target workflows
6. review checkout is available or explicitly excluded from MVP with a fallback plan
7. `gr1 1.0` release and deprecation framing are ready (`grip#581`)

And one explicit boundary condition:

8. identity-dependent behavior is not blocking the workspace cutover in OSS

If identity binding is required for a customer or internal workflow, that dependency must be satisfied by premium, not by adding identity logic to `grip`.

## Cutover Criteria Matrix

| Criterion | Owner | Evidence |
|---|---|---|
| MVP definition approved | Apollo / Track 1 | `grip#582` |
| Migration playbook approved | Atlas / Track 1 | `grip#522` |
| Real-git validation passes | Atlas / Track 1 | `grip#555` |
| Exec parity passes | Apollo / Track 1 | `grip#544` |
| Track 1 smoke suite green | Sentinel | Track 1 QA issue |
| Release framing ready | Opus | `grip#581` |
| Boundary lint green | CI + Sentinel | `grip#583` and recall equivalent |

## Cutover Procedure

Once the criteria are met:

1. announce cutover window
2. ensure all repos are synced
3. freeze destructive workspace changes briefly
4. verify `gr2` workspace spec and migration snapshots exist
5. run migration smoke suite
6. switch primary operator workflow to `gr2`
7. keep `gr1` available as a rollback path during the deprecation window

Rollback rule:

- if `gr2` fails a must-have workspace operation during cutover, return to `gr1` for that workflow and log the blocker

## Rollback Plan

Because migration is parallel-first, rollback is operationally simple:

- continue using `.gitgrip/`
- leave `.grip/` in place for debugging
- do not delete snapshots

Rollback does **not** require reconstructing the workspace from scratch unless the migration process itself corrupted state. That is why state preservation is mandatory in Stage 1.

## Premium Identity Boundary

This needs to be explicit because the last boundary violation happened here.

Allowed in `grip` migration:

- workspace detection
- manifest/state snapshotting
- repo and unit compilation
- materialization
- lane/workspace orchestration

Not allowed in `grip` migration:

- resolving who an agent is
- deriving persistent identity from filesystem layout
- encoding org routing
- migrating identity state into OSS workspace metadata

The correct wording in OSS documents and commands is:

- identity binding is handled by premium plugin or provider layers
- `grip` only migrates workspace mechanics

## Discrete Migration Tickets

This playbook decomposes into:

1. `grip#522`: migration playbook and cutover criteria
2. `grip#555`: real-git validation
3. `grip#544`: lane-aware exec parity
4. Track 1 QA issue: migration/apply/exec smoke suite
5. `grip#582`: gr2 MVP definition
6. `grip#581`: gr1 1.0 release and deprecation messaging

Optional supporting work:

- `grip#539`: apply convergence hardening
- `grip#546`: review checkout parity
- `grip#536`: manifest tooling only if needed for migration ergonomics

## Recommendation

Treat Sprint 23 as the point where `gr2` becomes cutover-credible, not automatically cutover-complete.

The path is:

1. define MVP
2. prove migration with real git
3. prove exec/apply/smoke parity
4. frame gr1 1.0 and deprecation
5. cut over workspace mechanics
6. leave identity migration to premium

# gr2 MVP: What Makes gr2 Shippable

This document defines the minimum feature set for gr2 to replace gr1 as the
primary workspace management tool. It draws the line between "MVP" (must ship)
and "post-MVP" (can follow), so we stop when we are done rather than when we
are tired.

## Decision Rule

gr2 is shippable when a user (human or agent) can complete a full workspace
lifecycle without falling back to gr1:

1. Define a workspace from a spec
2. Materialize repos from that spec
3. Create a lane for a task
4. Execute work in that lane (build, test, run)
5. Sync the workspace with upstream
6. Create and merge PRs
7. Migrate from an existing gr1 workspace

If any of these steps requires gr1, gr2 is not ready.

## MVP Feature Set

### Tier 1: Must Ship (blocks release)

These features gate the MVP. Without any one of them, the workflow above breaks.

| Feature | Module | Status | Issue |
|---------|--------|--------|-------|
| WorkspaceSpec schema + validation | spec_apply.py | Done | - |
| Plan dry-run (show what apply would do) | spec_apply.py | Done (Rust) | - |
| Apply materialization (clone/checkout repos from spec) | spec_apply.py | Partial | grip#539 |
| Lane CRUD (create, enter, exit, current) | app.py | Done | - |
| Lease acquire/release/show | app.py | Done | - |
| Exec lane-aware (run/test/build scoped to lane + repos) | execops.py | In progress | grip#544 |
| Sync workspace (fetch + rebase/merge for all repos) | syncops.py | Partial | - |
| Git primitives (clone, branch, checkout, status) | gitops.py | Done | - |
| Hook lifecycle (on_materialize, on_enter, on_exit) | hooks.py | Done | - |
| File projections (copyfile/linkfile from hook config) | hooks.py | Done | - |
| Event system (emit, outbox, cursor-based consumers) | events.py | Done | - |
| Failure markers (write, resolve, check unresolved) | failures.py | Done | - |
| gr1 migration (detect + migrate existing workspace) | migration.py | Done | - |

**What "done" means**: the module has real implementation (not stubs), handles
the primary use case, and has been proven in at least one test or prototype
scenario. It does not mean "production-hardened" or "feature-complete."

**What "partial" means**: the module exists with real code but has known gaps
that block the end-to-end workflow. These gaps are the MVP implementation work.

### Tier 2: Should Ship (strong default, can defer with justification)

These improve reliability and usability but do not break the core workflow if
missing at launch.

| Feature | Module | Status | Issue |
|---------|--------|--------|-------|
| Apply convergence (partially-materialized units) | spec_apply.py | In progress | grip#539 |
| Sync failure contract (stop/preserve/report semantics) | syncops.py | Designed | - |
| PR group orchestration (cross-repo linked PRs) | pr.py | Done | - |
| Platform adapter (GitHub API for PR/status/checks) | platform.py | Stub | - |
| Channel bridge (event-to-channel formatting) | channel_bridge.py | Done | - |

### Tier 3: Post-MVP (ship after release)

These extend gr2 but are not required for the replacement workflow.

| Feature | Notes |
|---------|-------|
| Lane checkout-pr (checkout PR branch into lane) | grip#546 |
| Multi-platform adapters (GitLab, Azure, Bitbucket) | platform.py stubs exist |
| Spec diffing (show drift between spec and workspace) | Rust plan command exists |
| Agent spawn integration | Premium; gr2 OSS provides the exec surface only |
| Repo maintenance policies | Prototype exists |
| CI/release surface | gr1 covers this; port later |
| Griptree management | gr1 covers this; port later |

## MVP Gaps: What Needs To Be Built

The audit (AUDIT-GR-VS-GR2.md) and module survey identify these gaps between
current state and Tier 1 completion.

### Gap 1: Exec lane-aware (grip#544)

**Current state**: `execops.py` has `run_exec()`, `acquire_exec_lease()`,
`release_exec_lease()`, and `exec_status_payload()`. The lease model works. The
app.py CLI wires `exec status` and `exec run`.

**What is missing**:
- `exec run` does not yet scope execution to the lane's repo set
- No parallel vs sequential execution policy
- No fail-fast vs collect-all behavior
- No structured result reporting (exit codes per repo)

**Acceptance**: a user can run `gr2 exec run -- cargo test` and have it execute
in every repo attached to the current lane, with clear per-repo output and a
summary exit code.

### Gap 2: Sync workspace

**Current state**: `syncops.py` has `SyncPlan`, `SyncOperation`, and
`SyncIssue` dataclasses. `build_sync_plan()` exists. `run_sync()` has a
signature but incomplete implementation.

**What is missing**:
- `run_sync()` implementation: fetch + merge/rebase per repo
- Dirty-repo handling (autostash policy from HOOK-EVENT-CONTRACT.md)
- Event emission (sync.started, sync.repo_updated, sync.repo_skipped,
  sync.conflict, sync.completed)
- Failure semantics per SYNC-FAILURE-CONTRACT.md

**Acceptance**: `gr2 sync run` fetches and integrates upstream for all repos in
the workspace, emits events for each repo, handles dirty repos per the stash
policy, and reports failures without silently losing state.

### Gap 3: Apply convergence (grip#539, Tier 2)

**Current state**: `spec_apply.py` materializes new units from scratch.
Planning does not detect missing nested repo checkouts inside an existing unit.

**What is missing**:
- Planning must emit an operation when declared unit repos are absent even if
  the unit directory and `unit.toml` exist
- Apply must clone/checkout the missing repos idempotently

**Acceptance**: running `gr2 apply` on a partially-materialized workspace
converges to the full declared state.

### Gap 4: Platform adapter (Tier 2)

**Current state**: `platform.py` defines the `PlatformAdapter` protocol with
`create_pr`, `merge_pr`, `pr_status`, `list_prs`, `pr_checks`. `GitHubAdapter`
is a stub.

**What is missing**:
- `GitHubAdapter` implementation using `gh` CLI or GitHub API
- Wiring `pr.py` group orchestration to the real adapter

**Acceptance**: `gr2 pr create` creates linked PRs across repos on GitHub and
`gr2 pr merge` merges them. This is Tier 2 because `gr1` covers PR workflow
today; gr2 can ship without its own PR surface as long as gr1 is still
installed.

## MVP Milestones

### M1: Core exec + sync (Sprint 23)

- [ ] grip#544: exec lane-aware implementation
- [ ] Sync run implementation (fetch + merge/rebase + events)
- [ ] Tests for exec and sync

### M2: Convergence + integration (Sprint 24)

- [ ] grip#539: apply convergence for partial units
- [ ] Platform adapter (GitHub, at minimum)
- [ ] End-to-end smoke test: init -> materialize -> lane -> exec -> sync -> PR

### M3: Migration validation (Sprint 24-25)

- [ ] Run gr2 migration on the synapt-dev gripspace
- [ ] Verify all daily workflows complete without gr1 fallback
- [ ] Document migration playbook

### Ship criteria

gr2 ships when M1 and M2 are complete and M3 validates the workflow. The ship
decision is explicit, not implicit: a ceremony PR from the team with sign-off
that the replacement workflow works end-to-end.

## Boundary Declaration

gr2 workspace orchestration is OSS. All features in this MVP definition live in
the grip repo. Identity resolution, org routing, agent identity, and workspace
policy enforcement live in premium (synapt-private) and connect through the
plugin seam.

The exec surface, sync engine, and materialization pipeline are neutral
infrastructure. They do not answer "who is this agent" or "what workspace owns
this." Any feature that crosses that line goes in premium per the identity test
heuristic.

## References

- AUDIT-GR-VS-GR2.md: Full command matrix comparing gr1 and gr2
- HOOK-EVENT-CONTRACT.md: Event system contract
- SYNC-FAILURE-CONTRACT.md: Sync failure semantics
- PR-LIFECYCLE.md: PR group orchestration design
- PLATFORM-ADAPTER-AND-SYNC.md: Platform adapter protocol
- SYNAPT-INTEGRATION.md: Premium integration boundary

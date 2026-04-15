# gr2 PR Lifecycle Management

This document defines how gr2 manages pull requests across multiple repos. It
builds on Atlas's PlatformAdapter protocol and references the event contract in
HOOK-EVENT-CONTRACT.md.

This is a **design document** for Sprint 20. It does not describe current
behavior; it defines the target design for `gr2 pr` commands.

## 1. Design Goals

- PR operations are cross-repo by default. `gr2 pr create` creates linked PRs
  across all repos with changes on the current lane's branch.
- A PR group is the first-class unit. Individual repo PRs are children of the
  group.
- PR state transitions emit events from the hook/event contract.
- PlatformAdapter is the only interface to the hosting platform. gr2 does not
  shell out to `gh`, `glab`, or platform-specific CLIs.
- Merge ordering is explicit and configurable, not implicit.

## 2. Concepts

### 2.1 PR Group

A **PR group** is a set of related PRs across repos that belong to the same
logical change. When an agent works on a lane that touches `grip`, `synapt`, and
`synapt-private`, `gr2 pr create` produces one PR group with three child PRs.

```json
{
  "pr_group_id": "pg_8a3f1b2c",
  "lane_name": "feat/hook-events",
  "owner_unit": "apollo",
  "created_by": "agent:apollo",
  "created_at": "2026-04-15T17:00:00+00:00",
  "title": "feat: hook/event contract design",
  "base_branch": "sprint-20",
  "head_branch": "design/hook-event-contract",
  "prs": [
    {
      "repo": "grip",
      "pr_number": 570,
      "url": "https://github.com/synapt-dev/grip/pull/570",
      "status": "open",
      "checks_status": "pending",
      "reviews": []
    },
    {
      "repo": "synapt",
      "pr_number": 583,
      "url": "https://github.com/synapt-dev/synapt/pull/583",
      "status": "open",
      "checks_status": "passing",
      "reviews": [{"reviewer": "sentinel", "verdict": "approved"}]
    }
  ]
}
```

The `pr_group_id` is the cross-repo correlation key from the event contract.
Format: `pg_` prefix + 8-char hex.

### 2.2 PR Group State

A PR group has an aggregate state derived from its children:

| Group State | Condition |
|-------------|-----------|
| `draft` | All child PRs are draft. |
| `open` | At least one child PR is open (non-draft). |
| `checks_pending` | At least one child PR has pending checks. |
| `checks_passing` | All child PRs have passing checks. |
| `checks_failing` | At least one child PR has failing checks. |
| `review_required` | At least one child PR needs more reviews to meet compiled review requirements. |
| `approved` | All child PRs meet their review requirements. |
| `mergeable` | All children are `checks_passing` + `approved` + no merge conflicts. |
| `merged` | All child PRs have been merged. |
| `partially_merged` | Some (but not all) child PRs have been merged. This is an error state. |

Group state is computed, not stored. `gr2 pr status` queries each child PR via
PlatformAdapter and aggregates.

### 2.3 PR Group Storage

PR group metadata is stored at:

```
.grip/pr_groups/{pr_group_id}.json
```

This file is created by `gr2 pr create` and updated by `gr2 pr status` (to
cache last-known state) and `gr2 pr merge` (to record merge SHAs).

The `.grip/pr_groups/` directory is workspace-local state, not committed to any
repo.

## 3. Commands

### 3.1 `gr2 pr create`

Creates linked PRs across repos.

```
gr2 pr create <workspace_root> <owner_unit> <lane_name>
    --title "feat: hook/event contract"
    [--body "description"]
    [--base sprint-20]
    [--draft]
    [--push]
    [--json]
```

Flow:

1. Load lane doc for `owner_unit/lane_name`.
2. For each repo in the lane's `repos` list:
   a. Check if the repo has commits on `head_branch` not in `base_branch`.
   b. Skip repos with no new commits (no empty PRs).
   c. Push the branch if `--push` is set.
   d. Call `PlatformAdapter.create_pr(repo, head, base, title, body, draft)`.
   e. Record the returned PR number and URL.
3. Generate `pr_group_id`.
4. Write PR group metadata to `.grip/pr_groups/{pr_group_id}.json`.
5. Update each child PR's body to include cross-links:
   ```
   ## Linked PRs (gr2 group: pg_8a3f1b2c)
   - synapt-dev/grip#570
   - synapt-dev/synapt#583
   ```
6. Emit `pr.created` event.
7. Print summary.

**Cross-linking** is important: each child PR's body includes references to all
sibling PRs. This makes the relationship visible on the platform even if gr2 is
not available.

**Base branch resolution**: If `--base` is not specified, use the lane's
`base_branch` (from lane doc) or fall back to the repo's default branch.

### 3.2 `gr2 pr status`

Shows aggregated status of the PR group for the current lane.

```
gr2 pr status <workspace_root> <owner_unit> [<lane_name>]
    [--json]
```

Flow:

1. Find the PR group for the lane (scan `.grip/pr_groups/` for matching
   `lane_name` and `owner_unit`).
2. For each child PR, call `PlatformAdapter.get_pr(repo, pr_number)`.
3. For each child PR, call `PlatformAdapter.get_checks(repo, pr_number)`.
4. For each child PR, call `PlatformAdapter.get_reviews(repo, pr_number)`.
5. Aggregate into group state.
6. Evaluate review requirements from compiled workspace constraints.
7. Print summary table:

```
PR Group pg_8a3f1b2c: feat: hook/event contract
Lane: apollo/design/hook-event-contract -> sprint-20

  Repo              PR    Checks    Reviews           Mergeable
  grip              #570  passing   1/1 required      yes
  synapt            #583  pending   0/1 required      no (checks pending)

  Group state: checks_pending
  Blocking: synapt checks pending
```

If any child PR has status changes since the last cached state, emit
`pr.status_changed` events.

### 3.3 `gr2 pr merge`

Merges the PR group.

```
gr2 pr merge <workspace_root> <owner_unit> [<lane_name>]
    [--strategy squash|merge|rebase]
    [--force]
    [--auto]
    [--json]
```

Flow:

1. Find the PR group for the lane.
2. Compute group state. If not `mergeable` and `--force` is not set, abort with
   an error explaining what is blocking.
3. Determine merge order (section 4).
4. For each child PR in order:
   a. Call `PlatformAdapter.merge_pr(repo, pr_number, strategy)`.
   b. Record the merge SHA.
   c. If merge fails, stop. Do not merge remaining repos. Emit
      `pr.merge_failed` event.
5. If all merges succeed, emit `pr.merged` event.
6. Update PR group metadata with merge SHAs and final state.
7. Print summary.

**`--auto` mode**: Instead of merging immediately, enable auto-merge on each
child PR. The platform merges each PR when its checks pass. This is useful for
CI-heavy repos where checks take time. Note: auto-merge relies on platform
support (GitHub has this; others may not).

**`--force` mode**: Skip the `mergeable` gate. Useful when a reviewer override
is needed. Still respects platform-level branch protection.

### 3.4 `gr2 pr checks`

Shows CI/check status for the PR group.

```
gr2 pr checks <workspace_root> <owner_unit> [<lane_name>]
    [--watch]
    [--json]
```

Flow:

1. Find the PR group.
2. For each child PR, call `PlatformAdapter.get_checks(repo, pr_number)`.
3. Print status per repo.

`--watch` mode: Poll every 30 seconds and update the display. Emit
`pr.checks_passed` or `pr.checks_failed` events when the aggregate state
changes.

### 3.5 `gr2 pr list`

Lists PR groups in the workspace.

```
gr2 pr list <workspace_root>
    [--owner-unit <unit>]
    [--state open|merged|all]
    [--json]
```

Flow:

1. Scan `.grip/pr_groups/` for group metadata files.
2. Filter by owner_unit and state.
3. Print summary table.

## 4. Merge Ordering

When merging a PR group, the order matters. If `synapt-private` depends on
`synapt`, merging `synapt-private` first could break CI on the base branch.

### 4.1 Default Order

Merge in `[[repos]]` declaration order from WorkspaceSpec. This is the simplest
model and works when the workspace author has already ordered repos by
dependency.

### 4.2 Explicit Order

The workspace spec can declare merge ordering:

```toml
[workspace_constraints.merge_order]
strategy = "explicit"
order = ["grip", "synapt", "synapt-private"]
```

### 4.3 Dependency-Aware Order (Future)

A future extension could parse repo dependency graphs (e.g., pip dependencies,
Cargo workspace members) to derive merge order automatically. This is out of
scope for Sprint 20.

### 4.4 Partial Merge Recovery

If merge fails partway through (repo A merged, repo B failed):

1. The PR group enters `partially_merged` state.
2. `pr.merge_failed` event is emitted for repo B.
3. The already-merged repo A cannot be un-merged.
4. Options:
   a. Fix the issue in repo B and retry `gr2 pr merge`.
   b. Revert repo A's merge manually and start over.

This is the most dangerous failure mode in cross-repo PR management. The design
doc acknowledges it but does not try to solve it automatically. The right
mitigation is:

- Run `gr2 pr checks` and confirm all checks pass before merging.
- Use `--auto` mode to let the platform gate each merge on checks.
- Keep the merge order aligned with dependency order so downstream repos
  merge after their dependencies.

## 5. PlatformAdapter Integration

### 5.1 Adapter Protocol (Atlas's Design)

gr2's PR lifecycle consumes Atlas's PlatformAdapter protocol. The expected
interface (from Atlas's `platform.py`):

```python
class PlatformAdapter(Protocol):
    def create_pr(self, repo: str, head: str, base: str,
                  title: str, body: str, draft: bool) -> PRRef: ...
    def get_pr(self, repo: str, pr_number: int) -> PRStatus: ...
    def merge_pr(self, repo: str, pr_number: int,
                 strategy: str) -> MergeResult: ...
    def get_checks(self, repo: str, pr_number: int) -> list[PRCheck]: ...
    def get_reviews(self, repo: str, pr_number: int) -> list[PRReview]: ...
    def update_pr_body(self, repo: str, pr_number: int, body: str) -> None: ...
```

**PlatformAdapter is group-unaware.** It operates on individual per-repo PRs and
has no concept of `pr_group_id` or cross-repo correlation. The grouping logic
lives in gr2's `pr.py` orchestration module, which:

1. Calls PlatformAdapter per-repo to create/query/merge individual PRs.
2. Assigns the `pr_group_id` (format: `pg_` + 8-char hex).
3. Correlates per-repo `PRRef` objects into a PR group.
4. Manages cross-link injection into PR bodies.
5. Emits `pr.*` events with the group ID.

This separation keeps platform adapters simple and reusable. A platform adapter
can be used by other tools that don't need grouping semantics.

### 5.2 Adapter Resolution

`get_platform_adapter(repo_spec)` resolves the correct adapter based on the
repo's remote URL. For Sprint 20, only `GitHubAdapter` is implemented.

### 5.3 Rate Limiting

The adapter handles rate limiting internally. If the platform returns a rate
limit response, the adapter retries with backoff. gr2 does not manage rate
limits at the PR lifecycle level.

### 5.4 Relation to gr1 HostingPlatform

gr1's Rust `HostingPlatform` trait (in `src/platform/traits.rs`) covers the same
operations. The Python PlatformAdapter is the gr2 equivalent, designed for
Python-first UX validation. When Rust gr2 absorbs PR lifecycle, it should
reuse the existing `HostingPlatform` trait, not create a third adapter surface.

The mapping:

| gr1 Rust trait | gr2 Python adapter |
|----------------|--------------------|
| `create_pull_request` | `create_pr` |
| `get_pull_request` | `get_pr` |
| `merge_pull_request` | `merge_pr` |
| `get_status_checks` | `get_checks` |
| `get_pull_request_reviews` | `get_reviews` |
| `update_pull_request_body` | `update_pr_body` |
| `find_pr_by_branch` | Not yet in adapter (needed for `gr2 pr status` without group ID) |
| `is_pull_request_approved` | Derived from `get_reviews` |

## 6. Event Emission

PR lifecycle emits events defined in HOOK-EVENT-CONTRACT.md section 3.2.

### 6.1 Create Flow Events

```
gr2 pr create
  -> pr.created (payload: pr_group_id, repos with pr_numbers)
```

### 6.2 Status Check Events

```
gr2 pr status (or --watch poll)
  -> pr.status_changed (per repo, when status differs from cached)
  -> pr.checks_passed (per repo, when all checks go green)
  -> pr.checks_failed (per repo, when a check fails)
  -> pr.review_submitted (per repo, when new review detected)
```

### 6.3 Merge Flow Events

```
gr2 pr merge
  -> pr.merged (if all repos merge successfully)
  or
  -> pr.merge_failed (for the first repo that fails)
```

### 6.4 Event Ordering

Events are emitted in operation order. For `gr2 pr merge` with repos A, B, C:

1. Merge A succeeds (no event yet; waiting for group completion).
2. Merge B succeeds (no event yet).
3. Merge C succeeds.
4. Emit `pr.merged` with all three repos' merge SHAs.

If merge B fails:

1. Merge A succeeds (no event for A alone).
2. Merge B fails.
3. Emit `pr.merge_failed` for B.
4. Do not attempt C.

The design emits one event at the end, not per-repo events during merge. This
keeps the event stream clean: consumers see either one `pr.merged` or one
`pr.merge_failed`, not a mix.

## 7. Review Requirements

### 7.1 Compiled Requirements

Review requirements come from the compiled WorkspaceSpec (originally from
premium's org policy):

```toml
[workspace_constraints.required_reviewers]
grip = 1
synapt = 1
synapt-private = 2
```

### 7.2 Evaluation

`gr2 pr status` evaluates review requirements per repo:

1. Get reviews from PlatformAdapter.
2. Count approvals (excluding stale reviews on outdated commits).
3. Compare against compiled requirement.
4. Report satisfied/unsatisfied per repo.

This already exists in the Python CLI as `gr2 review requirements`. The PR
lifecycle integrates it into the `mergeable` gate.

### 7.3 Boundary

Review requirement **evaluation** (counting approvals against a threshold) is
OSS. Review requirement **definition** (who can review, role-based overrides,
org-level policies) is premium. gr2 only consumes the compiled numeric
threshold.

## 8. Cross-Link Format

When `gr2 pr create` creates linked PRs, it appends a standard section to each
PR body:

```markdown
---

## gr2 PR Group: pg_8a3f1b2c

| Repo | PR |
|------|----|
| grip | synapt-dev/grip#570 |
| synapt | synapt-dev/synapt#583 |
| synapt-private | synapt-dev/synapt-private#291 |

Lane: `apollo/design/hook-event-contract`
Base: `sprint-20`

*Managed by [gr2](https://github.com/synapt-dev/grip)*
```

This section is:
- Machine-parseable (table format with consistent columns).
- Human-readable on GitHub/GitLab.
- Identifiable by the `gr2 PR Group:` header for updates.

When `gr2 pr status` detects a new child PR was added (e.g., a new repo was
added to the lane), it updates all sibling PR bodies to include the new link.

## 9. Lane Integration

### 9.1 Lane -> PR Group Mapping

A lane can have at most one active PR group. Creating a second PR group for the
same lane replaces the first (the old group is archived).

The mapping is:
- Forward: lane doc stores `pr_group_id` when a PR group is created.
- Reverse: PR group metadata stores `lane_name` and `owner_unit`.

### 9.2 Lane Exit with Open PRs

When `gr2 lane exit` is called while the lane has an open PR group:

- The lane exit proceeds normally (stash dirty state, run on_exit hooks).
- The PR group remains open. PRs are on the platform; they do not depend on
  the local lane state.
- `gr2 pr status` can still query the group even after the lane is exited.

### 9.3 Lane Archive after Merge

When `gr2 pr merge` completes successfully:

- The PR group is marked as `merged`.
- The lane is eligible for archival (`lane.archived` event).
- Actual archival (deleting the lane root, cleaning up branches) is a separate
  command or automated by spawn.

## 10. Relation to gr1

gr1's `gr pr create/status/merge/checks` commands are the production surface
today. They work but have implicit cross-repo linking (via branch name
convention, not explicit group IDs).

gr2's PR lifecycle improves on gr1 in three ways:

1. **Explicit grouping**: PR groups with stable IDs replace implicit branch-name
   matching.
2. **Event emission**: Every PR state change produces a durable event.
3. **Platform abstraction**: PlatformAdapter replaces direct `gh` CLI calls.

The migration path: gr1 continues to handle daily PR workflow until gr2's PR
commands are proven. gr2 PR commands are validated in the playground first
(Sentinel's QA arena), then adopted for real workflow.

## 11. Implementation Plan

### Sprint 20 (Design)

- This document.
- Event schema for pr.* types (done, in HOOK-EVENT-CONTRACT.md).
- Coordinate with Atlas on PlatformAdapter method signatures.
- Coordinate with Sentinel on QA scenarios for PR lifecycle.

### Sprint 21 (Implementation Target)

1. `gr2/python_cli/pr.py` module with PR group CRUD.
2. `gr2 pr create` command consuming PlatformAdapter.
3. `gr2 pr status` command with aggregated state.
4. `gr2 pr merge` command with ordering.
5. Event emission at each step.
6. Integration tests in QA arena.

### Sprint 22 (Polish)

- `gr2 pr checks --watch` with polling.
- `gr2 pr list` for workspace-wide PR overview.
- Auto-merge mode.
- Edge cases from QA arena feedback.

## 12. QA Arena Scenarios

These scenarios should be covered by Sentinel's adversarial test suite:

1. **Happy path**: Create PR group with 3 repos, all checks pass, all reviews
   met, merge succeeds.
2. **Partial merge failure**: Repo A merges, repo B has a conflict. Verify
   `partially_merged` state and `pr.merge_failed` event.
3. **Review requirement not met**: One repo needs 2 reviews, only has 1. Verify
   `gr2 pr merge` blocks (without `--force`).
4. **Stale review**: Review was approved, then new commits pushed. Verify the
   stale review is not counted.
5. **PR created with no changes in some repos**: Verify repos with no new
   commits are skipped, not given empty PRs.
6. **Rate limiting**: PlatformAdapter returns rate limit during `gr2 pr merge`.
   Verify retry behavior.
7. **Platform timeout**: PlatformAdapter times out during `gr2 pr status`.
   Verify graceful degradation (show cached state with warning).
8. **Concurrent merge**: Two agents try to merge the same PR group. Verify only
   one succeeds (platform-level atomicity).
9. **Cross-link update**: New repo added to lane after initial PR creation.
   Verify cross-links are updated in all sibling PRs.
10. **Auto-merge mode**: Enable auto-merge on all child PRs. Verify events are
    emitted when platform auto-merges each PR.

## 13. Open Questions

1. **PR group ID persistence**: Should `pr_group_id` be stored in the lane doc
   (tying it to local state) or only in `.grip/pr_groups/` (making it
   workspace-level state)? Current design uses both for forward/reverse lookup.
2. **Multi-platform groups**: Can a PR group span repos on different platforms
   (e.g., grip on GitHub, infra on GitLab)? The adapter-per-repo model supports
   this, but merge ordering and cross-linking become more complex.
3. **PR updates after creation**: Should `gr2 pr update` exist to change title,
   body, or base branch of an existing group? Or is that always done directly
   on the platform?
4. **Branch cleanup**: Should `gr2 pr merge` automatically delete remote
   branches after merge? gr1 does this. gr2 should probably follow suit but
   it is a destructive operation.
5. **Manifest repo PRs**: Should the manifest repo (if tracked) get its own
   child PR in the group? gr1 includes the manifest in PR operations. gr2's
   lane model may not always include the manifest.

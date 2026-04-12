# gr2 Needs and Criteria Rulebook

## Purpose

This document is the comprehensive criteria set for a first-class `gr2`
workspace that must work for:

- a solo human working across many repos
- multiple agents working in parallel
- mixed human + agent teams

It is not a feature wishlist. It is the rulebook for what the workspace must
be able to do without unsafe workarounds.

## Development Workflow

`gr2` should be developed with this sequence:

- design
- prototype
- verify
- repeat that loop until the shape holds
- build
- assess
- repeat for the next slice

The shorthand is:

- `(design -> prototype -> verify)^n`
- `build`
- `assess`
- `repeat`

For `gr2`, verification is not just a happy-path demo. It includes:

- adversarial scenarios
- real git behavior
- user-mode checks across solo human, single agent, multi-agent, and mixed
  human + agent workflows

## Primary Design Principle

The workspace unit is not a single repo checkout.

The workspace unit is:

- a named lane
- a selected repo set
- a branch/PR context
- a context bundle
- an execution surface

If `gr2` gets that right, humans and agents can work safely in parallel.
If it gets that wrong, users will fall back to ad hoc worktrees, raw git, and
hidden state.

## User Modes

`gr2` must support all of these without changing its core model.

### Solo Human

The user needs to:

- work across multiple repos as one feature
- review another PR without disturbing that feature
- start a second feature while the first waits on review
- run build/test/verify for the active multi-repo lane

The solo human should not need to understand or manage a shared repo cache.
If clone acceleration exists, it should feel like faster materialization, not a
second workspace concept.

### Single Agent

The agent needs to:

- discover the correct workspace context quickly
- understand which repos matter for the task
- branch and execute commands without guessing
- report deterministic status
- avoid clobbering unrelated work

The agent should receive explicit status about active working checkouts, not be
forced to reason about cache internals unless a clone/materialization problem
actually occurs.

### Multi-Agent Team

The team needs to:

- isolate active work per unit
- share durable context where appropriate
- preserve private context where appropriate
- coordinate across linked PRs and sprint lanes
- switch between tasks without contaminating each other's state

The team should benefit from shared clone acceleration, but the optimization
must not weaken private-workspace boundaries or turn `repos/<repo>` into a
confusing second place where work might be happening.

## Hard Requirements

### 1. Structural and Repo-State Separation

`gr2 apply` must remain structural only.

It may:

- create directories
- materialize missing repos
- attach repos into units
- converge partially-materialized lanes
- write metadata

It must not silently:

- pull
- merge
- rebase
- switch active branches
- discard or obscure dirty work

This is the most important safety boundary in the system.

### 2. Named Multi-Repo Lanes

The system must support named lanes as first-class objects.

Each lane must support:

- a repo set
- a branch map
- PR associations
- context roots
- execution defaults
- a lane type

Required lane types:

- `home`
- `feature`
- `review`
- `scratch`

### 3. Cheap Parallelism

Lane creation must be cheap enough that users do not avoid it.

That implies:

- shared cache for repo sources
- selective lane repo membership
- fast materialization from cache
- disposable review lanes

If lane creation is slow or expensive, users will bypass the model.

The cache is an implementation detail. The UX object remains the lane.

### 4. Shared and Private Context

The workspace must support two context scopes:

- shared workspace context
- unit-private context

Shared context must be durable and visible to all relevant workers.
Private context must not be treated as common workspace state.

Lane context should inherit both:

- shared workspace context
- unit-private context
- optional lane-local additions

### 5. Lane-Aware Execution

The workspace must provide lane-aware execution for:

- build
- test
- lint
- run
- verify

Execution must be scoped by:

- lane
- repo selection
- order/parallelism policy
- fail-fast or collect-all policy

If execution remains global, the lane model is incomplete.

### 6. Dirty Work Preservation

The system must never silently discard local modifications.

Required behaviors:

- detect dirty work explicitly
- block by default
- allow explicit preservation mode
- log preservation/recovery actions
- surface failed restore clearly

Autostash is acceptable only as an explicit preservation mechanism.

### 7. PR and Review Isolation

Users must be able to check out a PR without disturbing active feature work.

That means:

- PR checkout creates or updates a review lane
- review lanes are disposable
- review lanes can run their own commands
- review lanes do not mutate the current feature lane

### 8. Cross-Repo Feature Coherence

A cross-repo feature must be representable as one lane record, not merely
several repos that happen to share a branch name.

The lane must know:

- included repos
- per-repo branch intent
- associated PRs
- expected verification commands

### 9. Deterministic Status Surfaces

Agents and humans need trustworthy read surfaces.

`gr2` must provide explicit status views for:

- workspace structural drift
- repo sync drift
- lane metadata
- dirty state
- execution status
- PR linkage

Cache or transport state should only surface when it affects the user's ability
to materialize or repair a lane.

These should be machine-readable as well as human-readable.

### 9a. Structured Output Must Be First-Class

Machine-readable output should not be treated as an optional afterthought.

Agents routinely need:

- stable field names
- stable object shapes
- deterministic exit codes
- explicit next-step hints

So every status-style surface should support structured output as a first-class
mode, not a best-effort pretty-print after the human CLI is finished.

Required properties:

- all read surfaces support structured output
- structured output is versioned
- field names are stable across patch releases
- error output is also structured when structured mode is enabled
- command scope is explicit in the payload

Examples of required structured surfaces:

- `gr2 spec show`
- `gr2 plan`
- `gr2 repo status`
- `gr2 lane list`
- `gr2 lane show`
- `gr2 exec status`

The target user problem is simple:

- agents should not have to scrape prose
- humans should not lose readable output by default

### 10. Materialization Optimization Must Not Become A Second UX Model

`gr2 apply` may use shared local mirrors or reference clones as its materialization
substrate.

That is desirable for:

- speed
- disk reuse
- cheap review lanes
- adoptability on large workspaces

But the optimization must not become a user-facing mental model.

Required rule:

- users work in unit-local or lane-local checkouts
- `.grip/cache/repos/` is infrastructure
- status surfaces should describe active working checkouts first

If the design makes users reason about shared cache topology during normal work,
the UX has failed.

### 11. Strong UX Guidance

The product must teach the user which surface to use.

That means:

- CLI help must state command scope clearly
- status output should suggest likely next steps
- docs should include "use `gr2` for this, use git for that"
- common workflows should be expressed as short procedural paths
- structured output should be easy to enable and hard to forget

### 12. Multi-Repo Scratchpads

The system must support two or more temporary scratchpads simultaneously.

Examples:

- a feature lane
- a review lane
- a reproduction lane
- a release lane

This is not optional convenience. It is necessary for real workflow switching.

## Command-Surface Criteria

### Spec Surface

The user must be able to inspect and validate workspace intent without mutating
repo state.

Required surfaces:

- `gr2 spec show`
- `gr2 spec validate`
- later: `gr2 spec diff`, `gr2 spec explain`

### Structural Surface

The user must be able to preview and apply structural workspace changes.

Required surfaces:

- `gr2 plan`
- `gr2 apply`

### Repo-Maintenance Surface

The user must be able to inspect and update repo state explicitly.

Required surfaces:

- `gr2 repo status`
- `gr2 repo fetch`
- `gr2 repo sync`
- `gr2 repo checkout`

### Lane Surface

The user must be able to manage lanes directly.

Required surfaces:

- `gr2 lane list`
- `gr2 lane create`
- `gr2 lane status`
- `gr2 lane enter`
- `gr2 lane remove`
- `gr2 lane branch`
- `gr2 lane checkout-pr`

### Execution Surface

The user must be able to run multi-repo commands against a lane.

Required surfaces:

- `gr2 exec status`
- `gr2 exec run`
- `gr2 exec test`
- `gr2 exec build`

### Context Surface

The user must be able to inspect and manage effective context.

Required surfaces:

- `gr2 context show`
- `gr2 context shared edit`
- `gr2 context unit edit`

## Metadata Criteria

### Workspace Metadata

The workspace must persist:

- workspace identity
- spec version
- cache location
- repo registry
- unit registry

### Lane Metadata

Every lane must persist:

- lane id
- lane name
- owner unit
- lane type
- included repos
- branch map
- PR associations
- creation source
- context roots
- execution defaults
- recovery state

### Recovery Metadata

The workspace must persist enough state to explain and recover from:

- autostash actions
- failed restore
- interrupted sync/apply
- partially materialized lanes

## Agent-Specific Criteria

### Discoverability

An agent entering the workspace must be able to determine:

- what lane it is in
- what repos belong to that lane
- what commands are relevant
- what shared context applies
- what private context applies

without scraping ambiguous terminal output.

### Isolation

An agent must not need to infer which directories are safe to touch.

The workspace structure should make it obvious:

- shared context
- private unit roots
- lane-local checkouts

### Deterministic Automation

An agent must not have to guess whether:

- a merge succeeded
- a lane switched correctly
- a repo is safe to update
- a command should run in all repos or only some repos

This implies strong status and JSON-capable output surfaces.

## Human-Specific Criteria

### Low Ceremony

Humans should not need to think in terms of internal metadata to do common work.

The happy path should feel like:

- create lane
- switch into lane
- branch it
- run tests
- open PR
- switch to another lane

### Recoverability

When something goes wrong, the user must be able to answer:

- what lane am I in
- what repos are dirty
- what did the tool change
- where is my preserved work
- how do I get back to the prior state

## Criteria for Success

The design is successful if a user can do all of the following without raw git
or manual directory gymnastics:

1. Start a cross-repo feature in three repos.
2. Open linked PRs for that feature.
3. Leave the feature intact while it waits on review.
4. Check out another PR in a disposable review lane.
5. Start a second feature in a different lane.
6. Run build/test only for the correct lane and repo set.
7. Preserve dirty work safely while switching or syncing.
8. Return to the original feature lane and continue editing.
9. Do the same with two or more agents working simultaneously.

## Failure Criteria

The design has failed if users still need to rely on:

- ad hoc raw git worktrees across multiple repos
- hidden global sync behavior
- ambiguous context files
- manual stash/recovery folklore
- guessing which repo set a command should operate on
- chat memory instead of workspace state

## Implementation Priority

Build against this criteria set in this order:

1. narrow `apply`
2. `gr2 repo status`
3. lane metadata
4. `gr2 lane create/status/enter/remove`
5. `gr2 exec` lane-aware command surface
6. `gr2 lane checkout-pr`
7. `gr2 repo sync` with explicit preservation policy
8. richer context tooling

## Bottom Line

The bar for `gr2` is not "better multi-repo git commands."

The bar is:

- a trustworthy multi-repo operating system for humans and agents
- explicit context
- explicit execution
- explicit parallel work surfaces
- explicit recovery

That is the criteria rulebook. Any `gr2` design or implementation should be
judged against it.

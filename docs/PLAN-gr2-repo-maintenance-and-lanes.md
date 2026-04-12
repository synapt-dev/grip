# gr2 Repo Maintenance and Lanes

## Problem

`gr` taught us two useful things:

1. multi-repo work is real, not edge-case behavior
2. hiding repo coordination behind one broad `sync` surface creates avoidable confusion

The current pain points are consistent:

- manifest intent, file projection, and repo mutation are too entangled
- switching from one feature to another is too global
- reviewing a PR while keeping local feature work alive is awkward
- agents need more isolation than shared worktrees provide
- humans need less ceremony than a full fresh workspace for every task

We need `gr2` to be a first-class multi-repo workspace for both:

- a solo human working across several repos
- multiple agents working in parallel without stepping on each other

The workspace model needs to support:

- one feature spanning three repos
- a second feature starting while the first is in review
- checking out a PR without disturbing ongoing work
- keeping repo state explicit and recoverable
- preserving dirty local work by default

## Design Goals

- make workspace intent explicit
- make repo state transitions explicit
- make parallel work cheap
- make review and temporary work cheap
- preserve local modifications safely
- keep agent and human behavior on the same primitives
- avoid hidden pull/merge/rebase side effects

## Non-Goals

- `gr2 apply` is not a global "make it all right somehow" button
- `WorkspaceSpec v1` is not the place for full org/policy/runtime state
- shared repos and active feature sandboxes should not be conflated

## Core Split

`gr2` should separate three concerns.

### 1. Workspace Intent

`WorkspaceSpec` declares:

- which repos exist
- where they live
- which units use which repos
- basic workspace topology

This is the durable declarative layer.

### 2. Structural Convergence

`gr2 apply` should do only structural work:

- create missing directories
- materialize missing repo checkouts
- attach repos into units
- converge partially materialized units
- write unit metadata

It should not silently:

- pull from remotes
- merge branches
- rebase local work
- switch active branches unexpectedly

### 3. Repo Maintenance

Repo maintenance should be an explicit surface above structural apply:

- fetch
- fast-forward
- branch correction
- PR checkout
- autostash-based preservation
- divergence handling

That should become a separate command family, not hidden inside `apply`.

## Proposed Workspace Model

The workspace should have three layers:

```text
<workspace>/
├── .grip/
│   ├── workspace_spec.toml
│   ├── state/
│   └── cache/
│       └── repos/
├── config/
├── repos/
│   └── <shared checkouts>
├── agents/
│   └── <unit>/
│       ├── unit.toml
│       ├── home/
│       └── lanes/
│           ├── <lane-a>/
│           │   └── repos/
│           └── <lane-b>/
│               └── repos/
└── scratch/
    └── <review-or-throwaway-lanes>
```

### Shared Cache

`.grip/cache/repos/` stores reusable repo sources.

This is the acceleration layer:

- clone once
- materialize many
- cheap temporary sandboxes

### Shared Repos

`repos/` is for shared baseline or stable workspace-level repos.

These are not where active feature work should live by default.

### Unit Home

`agents/<unit>/home/` is the stable home lane for a person or agent.

This is where "my normal work" lives when not split into a feature lane.

### Lanes

A lane is a multi-repo scratchpad.

A lane contains:

- a selected set of repos
- a branch map
- local dirtiness state
- lane-local checkout paths
- review metadata if it came from a PR

This is the multi-repo equivalent of a worktree, but not tied to git's
single-repo worktree mechanism.

## Why Lanes Instead of Plain Worktrees

Git worktrees are repo-local. Our problem is workspace-wide.

One feature often means:

- repo A on `feat/x`
- repo B on `feat/x`
- repo C on `feat/x`

and the user wants that set to behave as one working context.

So the first-class unit is not "a checkout of one repo".
It is:

- one named lane
- one set of repos
- one branch intent
- one local review/edit surface

## User Flows

### 1. Solo Human, One Cross-Repo Feature

User wants to work on a feature spanning `app`, `api`, and `shared`.

```bash
gr2 lane create feat-auth --repos app,api,shared
gr2 lane branch feat-auth --name feat/auth
gr2 lane enter feat-auth
```

Now the user has one isolated multi-repo context.

### 2. Start Another Feature While the First Is in Review

The first feature is open in PR and waiting on review.
User wants to start a second feature without disturbing it.

```bash
gr2 lane create feat-billing --repos app,api
gr2 lane branch feat-billing --name feat/billing
gr2 lane enter feat-billing
```

The first lane stays intact. The second lane starts from cache-backed clones.

### 3. Check Out a PR While Keeping Your Own Work

User needs to review or patch someone else's PR while keeping their own lane.

```bash
gr2 lane checkout-pr review-541 --repo grip --pr 541
gr2 lane enter review-541
```

This should create a separate review lane. It must not disturb the user's
feature lane or home lane.

### 4. Agent Parallelism

Atlas is working on `feat-a`. Apollo is working on `feat-b`.
Both need isolated multi-repo state.

They should each have:

- private unit roots
- private lanes
- shared cache

That gives:

- low duplication where safe
- no checkout collisions
- no "another agent changed my branch" failures

### 5. Temporary Scratchpads

Yes, we should explicitly support two or more temporary scratchpads.

Examples:

- a feature lane
- a review lane
- a reproduction lane for a bug

Those should all be first-class, cheap, and disposable.

## Repo Maintenance Model

The prototype suggests this action taxonomy:

- `clone_missing`
- `block_path_conflict`
- `block_dirty`
- `autostash_then_sync`
- `checkout_branch`
- `fast_forward`
- `manual_sync`
- `no_change`

This is the right design direction because it makes repo state legible.

### Shared Repo Defaults

Shared repos can default to stricter automation:

- fetch allowed
- fast-forward allowed when clean
- branch correction allowed when no local work exists

### Lane Repo Defaults

Lane repos should be more conservative:

- no automatic branch movement
- no automatic merge/rebase
- stop when dirty unless explicit preservation is requested

This is the right default because lanes are where active work lives.

## Proposed Commands

### Structural

```bash
gr2 spec show
gr2 spec validate
gr2 plan
gr2 apply
```

### Repo Maintenance

```bash
gr2 repo status
gr2 repo sync
gr2 repo fetch
gr2 repo pull
gr2 repo checkout
```

### Lane Management

```bash
gr2 lane list
gr2 lane create <name>
gr2 lane enter <name>
gr2 lane leave
gr2 lane remove <name>
gr2 lane branch <lane> --name <branch>
gr2 lane checkout-pr <lane> --repo <repo> --pr <num>
gr2 lane status [<lane>]
```

## Branch and PR Behavior

### Branch Switching

Branch switching should be lane-local, not workspace-global.

If the user is in lane `feat-auth`, then:

```bash
gr2 lane branch feat-auth --name feat/auth
```

means:

- create or check out `feat/auth` across the repos in that lane
- do not disturb repos outside that lane

### PR Checkout

Checking out a PR should create or update a review lane.

That review lane should record:

- source repo
- PR number
- branch/ref used
- whether it is disposable

### Linked Cross-Repo Features

A feature spanning three repos should have one lane record that knows:

- which repos are included
- what branch each repo should be on
- which PRs belong to that lane

That is the real unit of work. Not "three separate branches that happen to
share a name."

## Relationship to `gr`

`gr` already has the right lessons:

- linked PRs matter
- synchronized branch flows matter
- atomic merge intent matters
- manifest-driven workspaces matter

But `gr` still carries too much global behavior.

`gr2` should keep the good parts and change the operating unit:

- from global repo sync
- to explicit workspace intent + lane-local repo maintenance

## Why This Should Hold Up Under Scrutiny

This design holds up if we enforce these rules:

### 1. `apply` stays narrow

If `apply` starts pulling, merging, rebasing, and switching branches, the model
collapses back into hidden side effects.

This rule is non-negotiable.

### 2. Lanes are cheap

If creating a lane feels expensive, people will go back to mutating one shared
workspace and the safety model will fail.

That means:

- shared cache
- fast materialization
- minimal ceremony

### 3. Lane state is explicit

Every lane needs durable metadata:

- included repos
- branch map
- PR associations
- creation source
- dirty/preserved state

If lane state is inferred ad hoc from the filesystem, users will not trust it.

### 4. Dirty-worktree handling is visible

Autostash is useful, but it is dangerous if hidden.

The system must record:

- stash creation
- target repo
- restore attempt
- restore failure

### 5. Shared repos and active work stay separate

If shared baseline repos and live feature repos share too much behavior, users
will be surprised by sync results.

### 6. PR workflows are first-class

If checking out a PR is treated as a second-class hack, users will keep opening
ad hoc worktrees and the multi-repo abstraction will fracture.

## Risks

### Disk Usage

Multiple lanes mean more checkouts.

Mitigation:

- shared cache
- sparse lane repo selection
- disposable review lanes

### Metadata Complexity

Lane metadata, branch maps, and PR associations can get messy.

Mitigation:

- durable lane manifest/state
- explicit commands
- strong status surfaces

### Too Many Abstractions

If we introduce `spec`, `apply`, `repo`, `lane`, `cache`, and `pr` without clear
boundaries, the UX will feel over-engineered.

Mitigation:

- keep the command split simple
- optimize the happy path
- document when to use each surface

## Practical Recommendation

Build `gr2` in this order:

1. finish narrow `apply`
2. add `gr2 repo status` using the prototype action taxonomy
3. define lane metadata format
4. add `gr2 lane create/status/enter/remove`
5. add `gr2 lane branch`
6. add `gr2 lane checkout-pr`
7. add `gr2 repo sync` with explicit policy and autostash logging

## Bottom Line

Yes, `gr2` should support two temporary scratchpads and more.

That is not optional polish. It is the core answer to:

- human multi-repo feature work
- agent parallelism
- review without disruption
- switching features while another waits in PR

The right foundation is:

- declarative workspace spec
- narrow structural apply
- explicit repo maintenance
- cheap named lanes as multi-repo scratchpads

That is meaningfully better than `gr`, and it uses the actual lessons we
already paid to learn.

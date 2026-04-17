# gr2 Lane Model Cross-Mode Stress Matrix

## Purpose

This is the adversarial verification matrix for the lane model itself.

`gr2` is not only a multi-repo tool. It is the operating surface for:

- a solo human
- a single agent
- multiple agents working in parallel
- mixed human + agent collaboration

The lane model is only credible if it holds under all four modes when users
are interrupted, confused, concurrent, or partially out of sync.

This document is intentionally hostile to the happy path.

## Core Stress Dimensions

Every lane design or implementation change should be tested against at least
these dimensions:

- recovery after interruption
- current-context ambiguity
- cross-lane contamination
- concurrent mutation
- private/shared boundary erosion
- machine-readable status quality
- wrong-surface choice
- stale lane cleanup
- review isolation
- escalation path to PR or durable implementation

## Solo Human Scenarios

### 1. Multiple active features plus one review lane

Scenario:
- user has three feature lanes open
- user switches to a review lane to inspect a PR
- later they forget which feature lane they were in before the review

What can break:
- the lane model becomes cognitively expensive
- review work disrupts the main task
- users fall back to ad hoc notes or shell history to recover state

What `gr2` must make obvious:
- current lane
- most recently active lanes
- branch intent per lane
- repo set per lane
- an explicit "return to previous lane" path

### 2. Human starts a second feature while the first waits on review

Scenario:
- feature A spans three repos and is waiting on peer review
- user starts feature B in two of the same repos

What can break:
- branch confusion across repos
- fear that new work will disturb the reviewable state of feature A

What `gr2` must guarantee:
- feature lanes stay isolated
- branch intent is visible per lane
- switching lanes does not require understanding cache internals

### 3. Human forgets whether a command should run in home or feature lane

Scenario:
- user returns after an interruption
- they know what they want to test, but not which lane owns the work

What can break:
- commands run in the wrong checkout
- users stop trusting lane-aware execution

What `gr2` must provide:
- one obvious lane status surface
- one obvious execution status surface
- enough next-step guidance to recover without spelunking the filesystem

## Single-Agent Scenarios

### 4. Agent is interrupted mid-task and must context-switch

Scenario:
- agent is mid-task in a feature lane
- a new prompt arrives and requires immediate work in another lane

What can break:
- agent loses its original working context
- agent resumes in the wrong lane later
- machine-readable state is too weak to recover deterministically

What `gr2` must provide:
- explicit current-lane status
- explicit recent-lane status
- machine-readable branch intent and repo membership
- deterministic lane re-entry

### 5. Agent must decide whether to use a feature lane, review lane, or scratchpad

Scenario:
- prompt asks for review, design, or implementation work
- agent has to choose a surface quickly

What can break:
- wrong-surface choice
- review work begins in a feature lane
- implementation begins in a scratchpad

What `gr2` must provide:
- strong recommendation surface
- clear distinctions between lane types
- structured output that can drive agent behavior without free-form guessing

### 6. Agent needs to report status without prose reconstruction

Scenario:
- supervising human or another agent asks what the current lane state is

What can break:
- status becomes a prose summary instead of a durable machine-readable fact

What `gr2` must provide:
- lane state in stable structured output
- repo set, branch intent, PR association, and execution defaults
- enough state for another worker to understand the lane without opening it

## Multi-Agent Scenarios

### 7. Two agents create different lanes that touch the same repo

Scenario:
- Atlas creates `feature/gr2-exec`
- Apollo creates `feature/gr2-materialize`
- both lanes include `grip`

What can break:
- cross-lane interference through shared active workspaces
- unclear ownership of repo mutations
- accidental reuse of one agent's private checkout

What `gr2` must guarantee:
- lane ownership remains explicit
- each agent gets its own working surface
- shared cache does not become shared mutable state

### 8. One agent reviews while another implements

Scenario:
- Apollo is actively implementing in a feature lane
- Atlas creates a review lane against the same repo/PR stack

What can break:
- review mutates the implementation lane
- ownership boundaries blur
- review lane becomes an unofficial shared worktree

What `gr2` must guarantee:
- review lane is isolated and disposable
- workspace-level discovery exists without sharing working directories

### 9. Agent handoff between workers

Scenario:
- one agent stops mid-lane
- another agent needs to understand the lane state and continue or review it

What can break:
- lane is understandable only via chat memory
- ownership transfer becomes filesystem archaeology

What `gr2` must provide:
- workspace-level discovery
- explicit unit ownership
- machine-readable lane summary
- explicit distinction between "resume in your own lane" and "inspect theirs"

## Mixed Human + Agent Scenarios

### 10. Human edits in a lane while an agent tries to execute in the same lane

Scenario:
- human is editing in a feature lane
- agent tries to run lane-aware exec against that same lane

What can break:
- hidden concurrent mutation
- trust collapse around agent actions

What `gr2` must answer explicitly:
- is same-lane concurrent execution allowed?
- if not, how is it blocked or redirected?
- if yes, what safety contract exists?

The default should favor safety and explicit conflict over silent concurrency.

### 11. Human wants help without giving up private working space

Scenario:
- human is implementing in a lane
- they want an agent to assist on related docs, verification, or review work

What can break:
- agent must enter the human's lane to help
- private-workspace boundaries erode

What `gr2` must provide:
- shared scratchpad for lightweight collaboration
- review lane for PR/inspection work
- clear rule that private implementation lanes stay private

### 12. Human and agent both lose track of the canonical context

Scenario:
- human thinks feature work lives in lane A
- agent thinks lane B is current because it was the last machine-visible lane

What can break:
- conflicting assumptions about where work lives
- commands and edits target the wrong lane

What `gr2` must provide:
- explicit current context markers
- explicit lane summaries for both human-readable and machine-readable use
- no hidden ambient lane inference that only one side can see

## Failure Criteria

The lane model is not ready if any of these remain fuzzy:

- how a user recovers the lane they were in before an interruption
- how an agent deterministically reports and resumes lane context
- how same-repo parallelism across agents stays isolated
- how mixed human + agent use avoids shared mutable lane state
- how users distinguish feature lanes, review lanes, and scratchpads under time pressure

If those answers are not obvious in both human-readable and machine-readable
surfaces, the model still needs prototype work before more build surface lands.

## Verification Gate

Before lane-heavy build work is treated as stable, we should be able to point
to repeatable prototype checks for:

- solo human recovery
- single-agent interruption and resume
- multi-agent same-repo parallelism
- mixed human + agent same-lane conflict handling

Until then, lane design should still be treated as under verification rather
than settled.

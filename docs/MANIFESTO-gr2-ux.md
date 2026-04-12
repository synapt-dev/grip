# gr2 UX Manifesto

## Why `gr2` Exists

Developers do not struggle because git is broken.

They struggle because modern work is larger than one checkout:

- one feature spans multiple repositories
- one review interrupts another active task
- one human works alongside multiple agents
- one team needs both private work areas and lightweight shared collaboration

`gr2` exists to make that multi-repo, multi-lane reality simpler, safer, and
more legible.

## Primary Thesis

`gr2` is not a git replacement.

`gr2` is a multi-repo workspace router.

It should make it easy to:

- start the right task context
- see which repos and branches belong to that context
- switch to another context without losing your place
- review, collaborate, and execute work in the right scope

Once the user is in the correct checkout, normal git should still feel normal.

## User-First Principles

### 1. The Primary Object Is The Task Context

Users think:

- "I am working on feature X"
- "I need to review PR Y"
- "I need a shared place to draft Z"

They do not think:

- "I need to manually coordinate three checkouts and remember which branch is
  active in each one"

So `gr2` must center:

- feature lanes
- review lanes
- shared scratchpads

Repos are part of the context. They are not the context.

### 2. Private Work And Shared Work Must Stay Separate

Users need both:

- private implementation surfaces
- shared collaboration surfaces

Private lanes protect active work.
Shared scratchpads make lightweight collaboration possible without violating
private workspace boundaries or paying the full cost of a PR.

The system must make that distinction explicit.

### 3. Use `gr2` To Choose Context, Use Git To Work

The intended user flow is:

1. use `gr2` to enter the correct lane or review context
2. use git normally inside the selected checkout
3. return to `gr2` when changing task, scope, or execution surface

If `gr2` tries to replace normal repo-local git, users will distrust it.
If `gr2` does not simplify multi-repo context changes, users will route around
it.

### 4. The Tool Must Never Hide Important State

Users should not have to guess:

- which repos are in scope
- which branches are intended
- which paths are active
- whether a command is structural or mutating
- whether local work is at risk

`gr2` should prefer explicit status over magical convenience.

### 5. Safety Beats Cleverness

`gr2 apply` must not silently:

- pull
- merge
- rebase
- switch branches
- discard dirty work

Users will trust `gr2` only if the safety boundary is simple and consistent:

- structural convergence belongs to `apply`
- repo maintenance belongs to explicit repo commands

## Human UX Requirements

A human should be able to:

- keep feature A active while feature B waits in review
- inspect PR C without disturbing either one
- collaborate on a blog post or spec without editing another person's private
  lane
- tell, quickly, which repos matter for the current task
- run the right build/test commands without reconstructing scope manually

## Agent UX Requirements

An agent should be able to:

- discover the current task context quickly
- consume stable machine-readable state
- know which repos matter without guessing
- avoid entering another worker's private directory
- choose the right next command from explicit status surfaces

This means structured output is not optional polish. It is first-class product
surface.

## Mixed Human + Agent Requirements

The same model must work for:

- a solo human
- one agent
- many agents
- one human working alongside many agents

That requires:

- one shared vocabulary for lanes, review, and scratchpads
- one shared status model
- private lanes by default
- shared collaboration surfaces when collaboration is the point

## What Good Looks Like

The user says:

- "start feature auth across app and api"
- "show me what this lane would run"
- "open a review lane for PR 548"
- "create a shared scratchpad for the sprint blog"
- "switch back to my feature without losing my place"

And the system responds with explicit, trustworthy state.

That is the bar.

## Product Test

When choosing a `gr2` feature, ask:

1. Does this make multi-repo context easier than ad hoc shell plus raw git?
2. Does this preserve private work while allowing shared collaboration?
3. Does this make the active task more legible?
4. Does this reduce guessing for both humans and agents?
5. Does this keep git normal once the user is in the correct checkout?

If the answer is not clearly yes, the design should be revised.

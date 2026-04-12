# gr2 Shared Scratchpads

## Problem

Private lanes are the right default for implementation work, but they leave a
collaboration gap:

- a PR is too heavy for early drafting
- another worker's private directory is the wrong place for joint editing
- ad hoc shared files have weak ownership and poor lifecycle

The blog workflow is the clearest example. Multiple workers may need to draft,
review, and refine shared content before anyone wants a formal PR.

## User Need

"I need a lightweight shared place to collaborate without crossing into someone
else's private workspace and without paying the full cost of a PR."

## Principle

Shared scratchpads are a sibling to private lanes, not an exception to them.

- private lanes stay private
- shared scratchpads are explicit shared workspace objects
- both should be cheap
- both should be inspectable

## First Slice

The first slice should be doc-first.

That keeps the initial surface focused on the actual pain:

- blogs
- RFCs
- release notes
- planning docs

It avoids turning "shared scratchpad" into an excuse for ambiguous shared code
ownership too early.

## Proposed Model

```text
<workspace>/
├── agents/
│   └── <unit>/
│       └── lanes/
│           └── ...
└── shared/
    └── scratchpads/
        └── <name>/
            ├── scratchpad.toml
            ├── docs/
            ├── notes/
            └── context/
```

## Scratchpad Metadata

Each shared scratchpad should record:

- name
- kind
- purpose
- participants
- linked issue / PR if any
- lifecycle state
- creation source
- default paths

Suggested kinds for the first pass:

- `doc`
- `review`
- `planning`

Suggested lifecycle states:

- `draft`
- `active`
- `paused`
- `done`

## Rules

Shared scratchpads should:

- be workspace-owned
- support multiple named participants
- make purpose explicit
- be easy to create and remove
- stay separate from private implementation lanes

Shared scratchpads should not:

- replace private feature lanes
- become the default place for multi-repo coding
- weaken the "do not enter someone else's private directory" rule

## UX Goals

The user should be able to:

1. create a shared scratchpad quickly
2. see who it is for
3. see what it is for
4. know whether it is still active
5. know what to do next

That implies explicit read surfaces:

- list scratchpads
- show one scratchpad
- suggest next step

## Prototype Goal

The prototype should answer:

- does a doc-first shared scratchpad actually reduce coordination friction?
- does the metadata feel sufficient?
- do users understand when to use a scratchpad instead of a PR or private lane?
- does this preserve the private-workspace safety model?

## Verification Gate

This prototype is only considered verified if it survives the adversarial
scenarios in:

- `docs/ASSESS-gr2-shared-scratchpads-stress.md`

That includes pressure around:

- concurrency
- stale state
- wrong-surface use
- lifecycle and cleanup
- graduation from scratchpad to real repo artifact / PR

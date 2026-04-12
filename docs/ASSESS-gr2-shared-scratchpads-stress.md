# gr2 Shared Scratchpads Stress Matrix

## Purpose

This is the break-case matrix for the shared scratchpad prototype.

The prototype is not "verified" because the happy path worked once.
It is only verified if it survives the scenarios most likely to fail in
real human + agent workflows.

This document is intentionally adversarial.

## Stress Dimensions

Every new `gr2` surface should be pressured along at least these dimensions:

- concurrency
- stale metadata
- ambiguous ownership
- partial cleanup
- wrong-surface use
- escalation path to PR / implementation lane
- recovery after interruption
- discoverability under pressure

## Shared Scratchpad Scenarios

### 1. Two users edit at once

Scenario:
- Atlas and Layne both use the same doc scratchpad
- both update the same draft in close succession

What can break:
- participants are recorded, but the workflow gives no clue how to coordinate
- a scratchpad quietly becomes a conflict zone

What the prototype must answer:
- does the metadata make shared ownership explicit?
- is the scratchpad clearly marked as shared rather than private?
- is there a legible next-step path when coordination is needed?

### 2. Scratchpad goes stale

Scenario:
- a blog draft scratchpad is created
- no one touches it for a week
- a new worker sees it later

What can break:
- no one knows if it is still active
- stale drafts clutter the workspace

Required design pressure:
- lifecycle state must be visible
- the model needs a pause/done path
- stale scratchpads must be distinguishable from active ones

### 3. Scratchpad scope creep into implementation

Scenario:
- users start dropping code or repo-specific implementation into a doc-first scratchpad

What can break:
- the scratchpad becomes an unofficial shared coding area
- private-lane boundaries erode

Required design pressure:
- doc-first scope must stay explicit
- the system must make it obvious that this is not a private feature lane
- there should be a clear escalation path into a real lane or PR

### 4. Wrong tool chosen

Scenario:
- user should have used a review lane, but instead creates a scratchpad
- or should have used a scratchpad, but opens a PR too early

What can break:
- the tool surface becomes confusing
- users stop trusting the model

Required design pressure:
- next-step guidance should help distinguish:
  - private lane
  - review lane
  - shared scratchpad
  - PR

### 5. Missing or wrong participants

Scenario:
- scratchpad is created without the right people listed
- later a third worker joins

What can break:
- implied ownership diverges from recorded ownership
- people edit without being visible in metadata

Required design pressure:
- participant list must be editable
- participant visibility must be cheap
- absence of participants should not imply private ownership

### 6. Scratchpad linked to the wrong issue or no issue

Scenario:
- scratchpad is linked to the wrong issue
- or there is no linked issue yet

What can break:
- scratchpad loses traceability
- coordination falls back into chat memory

Required design pressure:
- refs should be optional but visible
- it should be easy to add or fix refs later

### 7. Scratchpad completed, but content needs to graduate

Scenario:
- draft blog post is approved
- now it needs to become a proper repo artifact and PR

What can break:
- there is no obvious promotion path
- content gets stranded in the shared scratchpad

Required design pressure:
- the model needs a clear handoff path:
  - scratchpad -> repo file -> PR

### 8. Partial cleanup

Scenario:
- scratchpad metadata is removed, but docs remain
- or docs are removed, but metadata remains

What can break:
- orphaned shared state
- misleading listings

Required design pressure:
- cleanup needs to be explicit and symmetric
- the status surface must expose orphaned scratchpads

### 9. Discoverability under time pressure

Scenario:
- user just wants to collaborate on a blog now
- they should not have to reverse-engineer the whole workspace model

What can break:
- the feature is technically correct but too cognitively expensive

Required design pressure:
- one obvious create path
- one obvious list path
- one obvious "what now?" path

### 10. Private-workspace safety regression

Scenario:
- users begin treating scratchpads as permission to work in each other's spaces again

What can break:
- the original safety model regresses

Required design pressure:
- shared scratchpads must be framed as workspace-owned collaboration surfaces
- they must not blur into private lanes

## Gate Before MVP

Before `grip#553` should be considered ready to build or finalize, we should be
able to answer these questions clearly:

- how is a scratchpad different from a review lane?
- how is a scratchpad different from a PR?
- how is a scratchpad kept from turning into shared implementation sprawl?
- how does a stale scratchpad become visible and cleanable?
- how does content graduate from scratchpad to repo artifact?

If those are still fuzzy, the prototype is not ready for MVP.

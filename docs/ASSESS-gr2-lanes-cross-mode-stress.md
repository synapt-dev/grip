# gr2 Lane Model Cross-Mode Stress Matrix

This branch-local copy exists so the executable prototype work on `#556` has a
nearby assessment target.

The canonical docs version should live in the docs/rulebook lane, but the
prototype branch needs the same adversarial model while the design and
verification loop is still active.

The prototype harness for this document is:

- `gr2/prototypes/cross_mode_lane_stress.py`

It currently pressures four scenarios:

1. two agents create lanes that touch the same repo
2. human edits in a lane while an agent plans exec in the same lane
3. agent is interrupted mid-task and must recover lane context
4. solo human has three feature lanes, switches to review, then forgets which
   lane they were in before

The current expected outcomes are mixed by design:

- same-repo multi-agent lane creation should hold
- mixed same-lane human/agent execution should fail until occupancy or lease
  semantics exist
- interruption recovery should only partially hold until current/recent lane
  surfaces exist
- solo-human lane recovery should only partially hold until return-to-previous
  flow exists

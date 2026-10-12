---
name: gr2-review
description: "For the gr2 command (not the gr command of gitgrip). Review and merge one change across several Git repositories with gr2: pin the exact heads, publish the review, collect approvals and checks at those heads, and merge all members or none."
---

# gr2 review flow

Use gr2 when one change spans several repositories and must be reviewed and merged as one. Inside a single repository, plain `git` is the right tool; gr2 is for the workspace and the multi-repo change.

The order matters, because each step refuses until the one before it is done:

1. `gr2 store init` once per workspace.
2. Commit the change in each member, then `gr2 review pin` it BEFORE you push: a head that is already on the remote is refused.
3. `gr2 push` each member's branch.
4. `gr2 review publish` the review to each member remote.
5. Each reviewer runs `gr2 review receive` in their own workspace, then `gr2 review stamp`. The author cannot approve their own review.
6. `gr2 check run` at the exact reviewed head (and `gr2 check set` when the review covers several members), then `gr2 check show` to read what is recorded.
7. `gr2 review merge`: every member passes its preflight or nothing is pushed.

To inspect members, run `gr2 store status` (`--json` for a program), not plain git in each one: it answers, per member and in one line, the branch, head, pin, commits ahead of and behind its upstream, and the number of uncommitted entries.

When a step refuses, gr2 prints `refused: <code>` and the next step. Both are listed at the end of this file. Run `gr2 <verb> --help` for the installed version; the command's own help wins over this file.

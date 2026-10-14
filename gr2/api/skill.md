---
name: gr2-review
description: "For the gr2 command (not the gr command of gitgrip). Move a workspace of several Git repositories to another machine from one address, and review and merge one change across them with gr2: pin the exact heads, publish the review, collect approvals and checks at those heads, and merge all members or none."
---

# gr2: the workspace and the multi-repo change

Use gr2 when one piece of work spans several repositories. Inside a single repository, plain `git` is the right tool; gr2 is for the workspace and the multi-repo change.

## This page is the help

Everything an operator needs for the jobs below is on this page, and the reference at the end of this file is generated from this version's own code. Read `gr2 <verb> --help` only for a verb this page does not cover. If a verb refuses, read its refusal: it names the condition and usually prints the next command.

## Verb map (one line each)

| Job | Command (run where) |
|---|---|
| move a member to its remote's main | `git fetch -q origin && gr2 branch main --base origin/main` (member checkout) |
| stage and commit in one member | `gr2 add <paths>`, `gr2 commit -m "<msg>"` (member checkout) |
| record the whole workspace | `gr2 store commit -m "<msg>"` (workspace root) |
| confirm the record carries every pin | `gr2 store check` (workspace root) |
| show members, pins and record state | `gr2 store status` (workspace root) |
| push the record to one address | `gr2 store push --to <bare repo>` (workspace root) |
| rebuild every member from a record | `git clone <bare repo> <dest>`, then `gr2 store materialize` (in `<dest>`) |
| pin a review over one or more members | `gr2 review pin --rows-json rows.json` (workspace root) |
| publish, stamp, check, merge a review | `gr2 review publish`, `gr2 review stamp`, `gr2 check run`, `gr2 review merge` (see the review flow below) |

## Recipes: whole jobs, one script each

Each recipe is ONE script: write it once, run it once, and read its output at the end instead of probing between steps. Change only the lines marked EDIT. Every command in it is gr2 or plain shell.

### Recipe 1: bring every member to its remote main, then commit the same change in each

```bash
#!/bin/bash
set -euo pipefail
WS=/path/to/workspace                              # EDIT: the workspace root (it holds the member checkouts)
cd "$WS"
MEMBERS="alpha beta"                               # EDIT: the member directories
for m in $MEMBERS; do
  ( cd "$WS/$m"
    git fetch -q origin                            # gr2 has no fetch verb; this is the one Git step
    gr2 branch main --base origin/main             # main, at the remote's merged main
    mkdir -p pkg                                   # EDIT: the change, the same in every member
    printf 'def wip():\n    return "not pushed"\n' > pkg/wip.py
    gr2 add pkg/wip.py
    gr2 commit -m "$m: work in progress (not pushed)" )
done
for m in $MEMBERS; do echo "$m $(git -C "$WS/$m" rev-parse HEAD)"; done
```

### Recipe 2: record the whole workspace, unpushed member commits included, to ONE address

The record carries every member at its current commit, including commits no member remote has; nothing is pushed to any member remote. Run it from the workspace root after Recipe 1.

It needs a FULL-mode workspace: `mode = "full"` as a top-level key of the root's `grip.toml`, above its first `[table]`. A plain `gr2 store init` writes no top-level mode, which means compat, and in compat `store commit` refuses a member commit that is on no remote ("push it first"). For a new workspace, add that line before the first `store commit`; the script stops and says so when it is missing.

```bash
#!/bin/bash
set -euo pipefail
WS=/path/to/workspace                              # EDIT
ROOT=/path/to/root.git                             # EDIT: the one address (an empty bare repository)
cd "$WS"
awk '/^\[/{exit} {print}' grip.toml | grep -q '^mode *= *"full"' \
  || { echo 'grip.toml has no top-level mode = "full"; add it as the first line (see above), then run again' >&2; exit 1; }
gr2 store commit -m "workspace at its current commits"
gr2 store check                                    # every member's pin is carried by the record
gr2 store push --to "$ROOT"                        # pushes the record, and nothing to any member remote
gr2 store status
```

### Recipe 3: rebuild a workspace on a fresh machine from that one address

```bash
#!/bin/bash
set -euo pipefail
ROOT=/path/to/root.git                             # EDIT: the address the record was pushed to
DEST=/path/to/rebuilt                              # EDIT: where to rebuild
git clone -q "$ROOT" "$DEST"
cd "$DEST"
gr2 store materialize                              # checks out every member at its recorded commit
gr2 store status
for m in $(ls -d */ | tr -d /); do [ -d "$m/.git" ] && echo "$m $(git -C "$m" rev-parse HEAD)"; done
```

What these recipes do not need: `--help` on any of the verbs above, `git log` or `git ls-remote` to check progress (`store status` and `store check` answer it), or editing anything inside a member's remote.

## The review flow

The order matters, because each step refuses until the one before it is done:

1. `gr2 store init` once per workspace.
2. Commit the change in each member, then `gr2 review pin` it BEFORE you push: a head that is already on the remote is refused.
3. `gr2 push` each member's branch.
4. `gr2 review publish` the review to each member remote.
5. Each reviewer runs `gr2 review receive` in their own workspace, then `gr2 review stamp`. The author cannot approve their own review.
6. `gr2 check run` at the exact reviewed head (and `gr2 check set` when the review covers several members), then `gr2 check show` to read what is recorded.
7. `gr2 review merge`: every member passes its preflight or nothing is pushed.

To inspect members, run `gr2 store status` (`--json` for a program), not plain git in each one: it answers, per member and in one line, the branch, head, pin, commits ahead of and behind its upstream, and the number of uncommitted entries.

When a step refuses, gr2 prints `refused: <code>` and the next step. Both are listed at the end of this file, with every verb above and its options as this version defines them.

<!-- Generated by gr2/scripts/dump_skill.py from gr2's own code; edit the code or skill.intro.md, not this file. -->

## The verbs

### gr2 branch

Create or switch to a branch, natively -- no gr1 dependency.

- `<name>`
- `--base/--from/--start-point`: Create (or reset) the branch off this ref instead of current HEAD
- `--repo-path`: Repo to operate on (defaults to cwd; gr2 verbs are single-repo, not gripspace-wide)

### gr2 add

Stage paths in one repository, including tracked deletions.

- `<paths>`
- `--repo-path`: Repo to operate on (defaults to cwd; gr2 verbs are single-repo)
- `--lane`: Stage in EVERY repo of the entered lane (the workspace, unit and lane are read from where you are)

### gr2 commit

Create or amend a commit from the staged index.

- `--message/-m`: Commit message
- `--amend`: Amend the current commit
- `--repo-path`: Repo to operate on (defaults to cwd; single-repo)
- `--workspace-root`: Lane-aware: commit each repo of a lane in this workspace (requires --owner-unit)
- `--owner-unit`: Lane-aware: the unit whose lane to commit across
- `--lane`: Lane-aware: lane name (defaults to the unit's current lane)

### gr2 store init

Initialize a native store at cwd or the supplied root.

- `<workspace_root>` (optional)
- `--member`: Declare a member checkout by its path relative to the root (repeatable). Named paths win over the root's own spec and over sibling discovery; a named path that is not a checkout refuses the verb.
- `--clone`: Declare a member from a url: NAME=URL clones it at <root>/NAME and records it (repeatable). The root is created when it does not exist.
- `--json`: Emit machine-readable JSON

### gr2 store status

Show each member's branch, head, pin, ahead/behind its upstream, and uncommitted entries.

- `--json`: Emit machine-readable JSON

### gr2 store commit

Record member pins in the root git tree.

- `--message/-m`: Message for the root commit that records the member pins
- `--json`: Emit machine-readable JSON
- `--allow-rewind`: Record a member pin that is behind the one already recorded

### gr2 store check

Verify root gitlinks and member coverage against live upstreams.

- `--json`: Emit machine-readable JSON

### gr2 store push

Check then push only the root branch, never member branches.

- `--to`: Record this URL or path as the root's origin when it has none, then push
- `--json`: Emit machine-readable JSON

### gr2 store materialize

Materialize each canonical pin from its declared origin.

- `--json`: Emit machine-readable JSON

### gr2 review pin

Pin a review gr commit for one or more repository rows; print ``gr:<commit>``.

- `<workspace_root>` (optional)
- `--repo`: Repository key for a single pinned row
- `--remote`: Remote URL or absolute path of the row (relative paths and remote names are not resolved from your shell's directory)
- `--base`: Base SHA (must be the live remote head of --ref)
- `--head`: Reviewed head SHA (the pre-push head under review)
- `--ref` [default: refs/heads/dev]: Target ref whose live head must equal --base
- `--path`: Workspace path for the row (defaults to --repo)
- `--source`: Author clone holding the pre-push head; required to carry the range so `review open` can reconstruct
- `--from-range`: A frozen range.patch (freeze-public-range.sh output). Carries the range so `review open` reconstructs, deriving the head-tree by applying it over --base in a throwaway clone — NO author clone that holds the head is needed. Exclusive with --source.
- `--title`: Platform title text (NORM-hashed into the object)
- `--body`: Platform body text (NORM-hashed into the object)
- `--rows-json`: A JSON file with a list of row objects (key/remote/base/head, optional path/ref/title/body/source); pins ALL rows into ONE gr commit. Exclusive with the single-row flags.
- `--ratified`: Named ratify receipt id: the sanctioned fix-forward when a --head is already on the remote
- `--members`: With no rows given: pin only these members (comma-separated names)

### gr2 push

Push one branch and verify its immutable remote commit.

- `--remote`: Configured remote to push
- `--set-upstream/-u`: (no help text in the code)
- `--force-with-lease`: Replace the remote ref only if its observed value still matches
- `--repo-path`: Repo to operate on (defaults to cwd; gr2 verbs are single-repo)

### gr2 review publish

Publish the existing native review ref and verify its remote ID.

- `<workspace_root>` (optional)
- `<commit>`
- `--remote`: HTTPS URL or absolute local remote path
- `--ref`: Exact native review ref, otherwise derived from ID
- `--root/-C`: The workspace root. Same as the leading WORKSPACE_ROOT argument; give one or the other.

### gr2 review receive

Receive and validate an exact native review before pinning it locally.

- `<workspace_root>` (optional)
- `<commit>`
- `--remote`: HTTPS URL or absolute local remote path
- `--ref`: Exact native review ref, otherwise derived from ID
- `--root/-C`: The workspace root. Same as the leading WORKSPACE_ROOT argument; give one or the other.

### gr2 review stamp

Stamp the review: append an unsigned exact-head approval as git user.name, on every member remote.

- `<workspace_root>` (optional)
- `<review_id>` (optional)
- `--root/-C`: The workspace root. Same as the leading WORKSPACE_ROOT argument; give one or the other.

### gr2 check run

Execute at the exact commit and publish its observation.

- `<repo>`
- `<command>`
- `--remote`: Remote URL, path or name; default: the branch's upstream, else origin
- `--head`: Commit to check; default: HEAD
- `--name` [default: test]: (no help text in the code)

### gr2 check set

Run ONE command over every member of a pinned review, side by side at the pinned heads, and publish it.

- `<workspace_root>` (optional)
- `<review_id>`
- `<command>` (optional)
- `--name` [default: set]: Name the set record is stored under
- `--root-commit`: Check a DECLARED set (a [[sets]] table in grip.toml) at this root commit instead of a review
- `--set`: With --root-commit: the declared set to check
- `--at-remote-heads`: Check the review's members at the head this branch names on each member's remote NOW (after a move); recorded as remote_set, which no merge reads
- `--root/-C`: The workspace root. Same as the leading WORKSPACE_ROOT argument; give one or the other.

### gr2 check show

Read fresh remote observations for this exact commit.

- `<repo>`
- `--remote`: Remote URL, path or name; default: the branch's upstream, else origin
- `--head`: Commit to read; default: HEAD
- `--member` [default: repo]: (no help text in the code)
- `--require` [default: test]: (no help text in the code)
- `--review`: With --require set: count a `set` record only if it belongs to this review
- `--json`: (no help text in the code)

### gr2 review merge

Merge a pinned review into its members' remotes: preflight every member, merge nothing if any member fails it.

- `<workspace_root>` (optional)
- `<review_id>` (optional)
- `--into`: Target branch; default: the remote's default branch when it is the one branch at the reviewed base
- `--from`: Feature branch; default: the members' current branch
- `--check`: Required exact-head check name (repeatable); default `test`, and `test` plus `set` for a review over several members, where only `test` and `set` are accepted and `set` is always required
- `--no-set`: Single-member reviews only: a review over several members always requires `set` and refuses this flag
- `--approvals`: Required distinct approvers; may raise the workspace approvals.required floor, default 0
- `--root/-C`: The workspace root. Same as the leading WORKSPACE_ROOT argument; give one or the other.

## When gr2 prints `refused: <code>`

gr2 names the next step beside the refusal; the same text is here, keyed by the code. A code ending in `*` covers every code that starts with it and has no row of its own.

### approvals_insufficient

next: more approvers are required; each one stamps from their own checkout: gr2 review stamp '<workspace>' '<review>'

### base_moved

next: the target moved after the pin; pin again on the new base with gr2 review pin

### check_absent

next: gr2 check run '<path>' --remote '<remote>' --head '<head>' -- <your test command>

### check_fail

next: the check failed at this head; fix it, commit a new head, then pin, stamp, check and publish that head

### check_set*

next: gr2 check set '<workspace>' '<review>' -- <your combined test command>

### check_set_fail

next: the set check failed at these heads and that failure is recorded; a rerun does not clear it. Fix the member, commit a new head, then pin, stamp, run gr2 check set on that head and publish it

### feature_moved

next: the feature branch moved after the pin; to check the set as it is now: gr2 check set '<workspace>' '<review>' --at-remote-heads '<branch>' -- <your combined test command>. To merge it, commit a new head and pin it before you push, or pin this head with --ratified <receipt id> naming the ratify that covers it

### head_already_on_remote

next: pin a head before you push it. For this head, either commit a new head and pin it before pushing, or pin this one with --ratified <receipt id> naming the ratify that already covers it

### head_not_fetched

next: the member remote '<remote>' does not serve the reviewed head '<head>'; once the author has pushed it there, stamp again: gr2 review stamp '<workspace>' '<review>'

### not_received

next: receive the published review into this workspace, then stamp it: gr2 review receive '<workspace>' '<review>' --remote <member-remote>, then gr2 review stamp '<workspace>' '<review>'

### platform_head_moved_after_merge

next: this member landed at the pinned head, and the later commit <sha> is on the PR's branch only: review it as a new change (pin it on top of the merged target), or remove it from the branch; members after this one were not landed, so run gr2 review merge '<workspace>' '<review>' again once it is resolved

### platform_pr_ambiguous

next: close the extra PRs so one open PR has this head, then run the merge again; nothing was merged

### platform_pr_not_ready

next: mark the PR ready (or reopen it) on the platform, then run the merge again; nothing was merged

### platform_unmeasurable

next: install the GitHub CLI (gh) and authenticate it (gh auth login), then run the merge again; nothing was merged

### review_not_on_member_remote

next: gr2 review publish '<workspace>' '<review>' --remote '<remote>'

### self_approval

next: a different approver stamps it from their own workspace, after receiving it there: gr2 review receive <their-workspace> '<review>' --remote <member-remote>, then gr2 review stamp <their-workspace> '<review>'

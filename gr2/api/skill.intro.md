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

### Recipe 4: run the test that counts over a whole review, at its pinned heads

`gr2 check set` checks out every member of the review side by side at its pinned head, runs ONE command once at that lane's root, and records the result on every member's remote as the check named `set`. Every member's clone must already hold its pinned head, so the recipe fetches first; without that, `check set` stops with `could not get object info` (exit 2). Write the command as a small script; it runs with `GR2_SET_ROOT` (the lane root, holding one directory per member) and `GR2_SET_MEMBERS` (JSON, member key to path) set.

```bash
#!/bin/bash
set -euo pipefail
WS=/path/to/workspace                              # EDIT: your workspace root
REVIEW=gr:0000000000000000000000000000000000000000 # EDIT: the full review ID that `gr2 review pin` printed
MEMBERS="alpha beta"                               # EDIT: the member directories
for m in $MEMBERS; do git -C "$WS/$m" fetch -q origin; done   # the pinned heads must be in your clones
T=$(mktemp -d)                                     # outside the workspace, so the record stays clean
cat > "$T/set-test.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd "$GR2_SET_ROOT"
for m in alpha beta; do                            # EDIT: the member directories
  ( cd "$m" && python3 -m pytest -q -p no:cacheprovider )   # EDIT: each member's test command
done
EOF
chmod +x "$T/set-test.sh"
gr2 check set "$WS" "$REVIEW" -- "$T/set-test.sh"  # exit 0 pass, 1 fail (it prints the output tail), 2 could not run
```

### Recipe 5: approve a teammate's review

Run it in YOUR workspace: the approval is made as that workspace's `git user.name`, and the author cannot approve their own review. It runs the set test itself (as in Recipe 4) and stops before stamping when that test fails.

```bash
#!/bin/bash
set -euo pipefail
WS=/path/to/your/workspace                         # EDIT: your workspace root
REVIEW=gr:0000000000000000000000000000000000000000 # EDIT: the full review ID the author sent you
REMOTES="/path/to/alpha.git /path/to/beta.git"     # EDIT: every member's remote
MEMBERS="alpha beta"                               # EDIT: the member directories, in the same order
for r in $REMOTES; do gr2 review receive "$WS" "$REVIEW" --remote "$r"; done
for m in $MEMBERS; do git -C "$WS/$m" fetch -q origin; done   # receive does not fetch the heads; the set test needs them
gr2 review show "$WS" "$REVIEW"                    # each member's commit range and the files it changes
# READ the change before going on: in each member, `git diff <range start>..<range end>` for the range shown above
T=$(mktemp -d)
cat > "$T/set-test.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd "$GR2_SET_ROOT"
for m in alpha beta; do                            # EDIT: the member directories
  ( cd "$m" && python3 -m pytest -q -p no:cacheprovider )   # EDIT: each member's test command
done
EOF
chmod +x "$T/set-test.sh"
gr2 check set "$WS" "$REVIEW" -- "$T/set-test.sh"  # a failing set test exits 1 here, so nothing below runs
gr2 review stamp "$WS" "$REVIEW"                   # the approval, at exactly the pinned heads, on every member remote
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

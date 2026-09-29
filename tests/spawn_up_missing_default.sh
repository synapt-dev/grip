#!/usr/bin/env bash
# Integration test: gr spawn up missing-agents-only default + worktree-born
# windows + no half-spawned duplicates on error.
#
# Fixtures are the 2026-09-16 incidents (private-tracker class):
#   (1) bare `gr spawn up` relaunched ALL agents and clobbered a live window;
#   (2) a respawned window sat in the caller's cwd until the launch script's
#       cd took effect, so a pane was born at the gripspace root;
#   (3) the error path left a half-spawned duplicate window behind and
#       aborted before reaching the remaining agents.
#
# Runs entirely on a PRIVATE tmux server (unset TMUX + a throwaway
# TMUX_TMPDIR), so nothing this test spawns or kills can touch the default
# socket where the live fleet runs (see spawn_graceful_shutdown.sh for the
# incident record). Proven on this host: a session on the private socket is
# invisible to the default `tmux ls`.
#
# Requires: tmux, gr (built). Run: GR=./target/debug/gr ./tests/spawn_up_missing_default.sh

set -euo pipefail

unset TMUX
TMUX_TMPDIR="$(mktemp -d)"
export TMUX_TMPDIR

# this leak: this test drives `gr spawn up --mock`, and that path writes agent routing
# records through SYNAPT_AGENT_PANES_FILE, which every real agent shell exports pointing at the
# LIVE ~/.synapt/agent-panes.json. Unisolated, this test's fixtures land in that file and
# `speak_to_agent` resolves by LAST-WRITE-WINS with no freshness or liveness tie-break — so a
# fixture named like a real agent can shadow it, which is the live pane-map leak measured exactly.
# `unset` is sufficient and correct rather than a scratch path: spawn.rs:1009 is
# `if let Some(path) = routing_file_path()`, and routing_file_path() (spawn.rs:293) returns None
# for an unset or empty value, so the whole block is skipped — no write, no error.
unset SYNAPT_AGENT_PANES_FILE

# WITNESS: the live file must be unchanged across this test. A silent fixture write is exactly what
# the live pane-map leak was, so the check is a CONTENT digest, not a line count.
# WHY A DIGEST RATHER THAN A LINE COUNT (corrected: my first version gave a reason that does not
# hold, and BOTH readers caught it independently). This file is 1338 lines of pretty-printed JSON,
# so an added key DOES add a line and a line count would catch the leak's basic shape too. The real
# discrimination is that a digest of sorted key->value pairs ignores FORMATTING AND ORDERING, which
# a line count does not. My original clause -- "an upsert of an unchanged record moves no line
# count" -- described a no-op, which does no harm by definition.
# ⚠ AND IT MUST COVER VALUES, NOT ONLY KEYS (Sentinel, r1; confirmed by Atlas, r1). Hashing
# sorted(json.load(f)) hashes only the KEYS, because iterating a dict yields keys -- so an upsert of
# an EXISTING key with a DIFFERENT VALUE changes no key, leaves the digest identical and the
# witness GREEN while a live agent's routing record is corrupted. That is the leak's actual harm
# with nothing moving. The harmful upsert is precisely the value-changing one.
# KNOWN LIMIT, named rather than implied: a content-identical rewrite is invisible here. So this is
# exact for what it was built for and is not a general "was the file written" witness.
PANES_FILE="$HOME/.synapt/agent-panes.json"
panes_digest() {  # content digest of the live file, or a sentinel when it is absent
    if [ -f "$PANES_FILE" ]; then
        python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("\n".join(f"{k}\t{json.dumps(v, sort_keys=True)}" for k, v in sorted(d.items())))' \
            "$PANES_FILE" 2>/dev/null | shasum -a 256 | cut -c1-16
    else
        echo "absent"
    fi
}
PANES_DIGEST_BEFORE="$(panes_digest)"

GR="${GR:-./target/debug/gr}"
case "$GR" in /*) ;; *) GR="$(cd "$(dirname "$GR")" && pwd)/$(basename "$GR")" ;; esac
SESSION="spawnfix-$$"
FAILURES=0

cleanup() {
    tmux kill-server 2>/dev/null || true
    rm -rf "$TMUX_TMPDIR"
}
trap cleanup EXIT

assert_eq() { # assert_eq <name> <got> <want>
    if [ "$2" != "$3" ]; then
        echo "FAIL [$1]: got '$2', want '$3'"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS [$1]"
    fi
}

assert_contains() { # assert_contains <name> <haystack> <needle>
    if ! grep -qi "$3" <<<"$2"; then
        echo "FAIL [$1]: '$3' not found in: $2"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS [$1]"
    fi
}

# --- Build the fixture gripspace ---------------------------------------------
ROOT="$(mktemp -d)"
WORKTREES="$(dirname "$ROOT")"
mkdir -p "$ROOT/.gitgrip" "$ROOT/.gitgrip/prompts" "$(dirname "$ROOT")/beta-tree-$$" "$(dirname "$ROOT")/gone-tree-$$"
# 'gamma' points at a worktree that will be DELETED before the error run.

cat > "$ROOT/.gitgrip/agents.toml" <<EOF
[spawn]
session_name = "$SESSION"
channel = "general"
auto_journal = false
mock_launch = true

[agents.alpha]
role = "Fixture coordinator"
model = "example-model"
tool = "claude"
worktree = "main"
startup_prompt = ".gitgrip/prompts/alpha.md"
loop_interval = "1m"
env = {}

[agents.beta]
role = "Fixture worker"
model = "example-model"
tool = "claude"
worktree = "beta-tree-$$"
startup_prompt = ".gitgrip/prompts/beta.md"
loop_interval = "1m"
env = {}

[agents.gamma]
role = "Fixture casualty"
model = "example-model"
tool = "claude"
worktree = "gone-tree-$$"
startup_prompt = ".gitgrip/prompts/gamma.md"
loop_interval = "1m"
env = {}
EOF
echo "alpha prompt" > "$ROOT/.gitgrip/prompts/alpha.md"
echo "beta prompt" > "$ROOT/.gitgrip/prompts/beta.md"
echo "gamma prompt" > "$ROOT/.gitgrip/prompts/gamma.md"

pane_path() {
    tmux display-message -p -t "$SESSION:$1" '#{pane_current_path}' 2>/dev/null || echo ""
}

window_count() {
    tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -cx "$1" || true
}

# --- Run 1: initial spawn (mock) ----------------------------------------------
echo "== run 1: bare spawn up creates all three agents, each born in its worktree =="
(cd "$ROOT" && "$GR" spawn up --mock > "$TMUX_TMPDIR/run1.log" 2>&1) || {
    echo "FAIL [run1 exits]: see $TMUX_TMPDIR/run1.log"
    FAILURES=$((FAILURES + 1))
}

assert_eq "alpha window exists after run 1" "$(window_count alpha)" "1"
assert_eq "beta window exists after run 1" "$(window_count beta)" "1"
assert_eq "alpha pane born in its worktree (incident 2)" "$(pane_path alpha)" "$(cd "$ROOT" && pwd -P)"
assert_eq "beta pane born in its worktree (incident 2)" "$(pane_path beta)" "$(cd "$(dirname "$ROOT")/beta-tree-$$" && pwd -P)"

ALPHA_PID_RUN1="$(tmux display-message -p -t "$SESSION:alpha" '#{pane_pid}')"

# --- Run 2: bare up again must touch nothing (missing-agents-only) -------------
echo "== run 2: bare spawn up skips already-running agents (incident 1) =="
(cd "$ROOT" && "$GR" spawn up --mock > "$TMUX_TMPDIR/run2.log" 2>&1) || true
assert_eq "no duplicate alpha window (incident 1)" "$(window_count alpha)" "1"
assert_eq "no duplicate beta window" "$(window_count beta)" "1"
assert_eq "alpha pane untouched by run 2" \
    "$(tmux display-message -p -t "$SESSION:alpha" '#{pane_pid}')" "$ALPHA_PID_RUN1"
assert_contains "run 2 reports skipping" "$(cat "$TMUX_TMPDIR/run2.log")" "already running"

# --- Run 3: --force is the explicit full relaunch -------------------------------
echo "== run 3: --force relaunches explicitly =="
(cd "$ROOT" && "$GR" spawn up --mock --force > "$TMUX_TMPDIR/run3.log" 2>&1) || {
    echo "FAIL [run3 exits]: see $TMUX_TMPDIR/run3.log"
    FAILURES=$((FAILURES + 1))
}
assert_eq "alpha window exactly one after --force" "$(window_count alpha)" "1"
assert_eq "alpha pane born in its worktree after --force (incident 2)" "$(pane_path alpha)" "$(cd "$ROOT" && pwd -P)"

# --- Run 4: missing worktree refuses BEFORE any window exists (incident 3) ------
echo "== run 4: unlaunchable agent refuses without half-spawned windows =="
# gamma is NOT running entering this run (its window from run 1 is removed),
# and its worktree has vanished: the whole run must refuse in pre-flight,
# before any window is created or replaced.
tmux kill-window -t "$SESSION:gamma" 2>/dev/null || true
rmdir "$WORKTREES/gone-tree-$$"
RUN4_RC=0
(cd "$ROOT" && "$GR" spawn up --mock --force > "$TMUX_TMPDIR/run4.log" 2>&1) || RUN4_RC=$?
assert_contains "run 4 names the missing worktree" "$(cat "$TMUX_TMPDIR/run4.log")" "gone-tree"
assert_eq "no window left behind for gamma (incident 3)" "$(window_count gamma)" "0"

echo
# --- the live pane-map leak witness: the live routing file's CONTENT DIGEST is untouched -------
# A silent fixture write is what the live pane-map leak was, so this compares a digest of sorted
# key->value pairs, not a line count and not a key set. The digest is the right instrument because
# it ignores FORMATTING AND ORDERING, which a line count does not; and it covers VALUES, which a
# key set does not -- an upsert of an existing key with a changed value moves no key and would be
# invisible to a key-set check. Both halves are stated here as well as above, deliberately: the
# first version of this file carried the correction in the BEFORE block only, so the block that
# RUNS the check still taught the retracted reasoning (Sentinel, r2).
PANES_DIGEST_AFTER="$(panes_digest)"
if [ "$PANES_DIGEST_BEFORE" != "$PANES_DIGEST_AFTER" ]; then
    echo "FAIL [the live pane-map leak]: the live agent-panes CONTENT digest moved across this test"
    echo "  before: $PANES_DIGEST_BEFORE"
    echo "  after:  $PANES_DIGEST_AFTER"
    echo "  (a moved digest means either a key or an existing key's value changed -- check both)"
    echo "  this test must not write $PANES_FILE; confirm SYNAPT_AGENT_PANES_FILE is still unset"
    FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -gt 0 ]; then
    echo "FAILED: $FAILURES assertion(s)"
    exit 1
fi
echo "ALL PASS"
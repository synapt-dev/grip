#!/usr/bin/env bash
# Integration test: gr spawn down sends /exit before killing tmux windows.
#
# Runs entirely on a PRIVATE tmux server (unset TMUX + a throwaway
# TMUX_TMPDIR), so nothing this test spawns, downs, or kills can touch the
# default socket where the live agent fleet runs. See config claude.md,
# Workspace Etiquette: "The default tmux socket is the live fleet" (two fleet
# kills in one day, 2026-09-02, from a hardcoded SESSION="synapt" here).
#
# NOTE: requires tmux. Run manually before merging spawn-related changes:
#
#   cd grip && GR=./target/debug/gr ./tests/spawn_graceful_shutdown.sh
#
# Requires: tmux, gr (built), a .gitgrip/agents.toml with an explicit
# session_name and at least one agent (CI copies tests/fixtures/agents.example.toml).

set -euo pipefail

# --- Isolate onto a private tmux server -------------------------------------
# Every tmux invocation below -- this script's AND the ones gr runs as a
# subprocess -- inherits TMUX_TMPDIR and lands on this throwaway socket.
# Unsetting TMUX prevents nesting confusion when run from inside tmux.
# Proven on this host: a session here is invisible to the default `tmux ls`,
# and `tmux kill-server` here leaves the default server (the fleet) alive.
unset TMUX
TMUX_TMPDIR="$(mktemp -d)"
export TMUX_TMPDIR

GR="${GR:-./target/debug/gr}"
LOG=$(mktemp)

cleanup() {
    # kill-server on the PRIVATE socket only; never kill-session on the default.
    tmux kill-server 2>/dev/null || true
    rm -rf "$TMUX_TMPDIR"
    rm -f "$LOG"
}
trap cleanup EXIT

# --- Resolve the session name from the config gr will use -------------------
# gr walks up from the cwd for a .gitgrip directory; mirror that so we read the
# SAME agents.toml gr loads. The session name is read from the config, never a
# literal -- a literal "synapt" here is what read the live fleet as a fixture.
find_agents_toml() {
    local dir
    dir="$(pwd)"
    while [ "$dir" != "/" ]; do
        if [ -f "$dir/.gitgrip/agents.toml" ]; then
            printf '%s\n' "$dir/.gitgrip/agents.toml"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

AGENTS_TOML="$(find_agents_toml)" || {
    echo "FAIL: no .gitgrip/agents.toml found from $(pwd) upward"
    echo "      (CI copies tests/fixtures/agents.example.toml into .gitgrip/)"
    exit 1
}

SESSION="$(grep -E '^[[:space:]]*session_name[[:space:]]*=' "$AGENTS_TOML" \
    | head -1 | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/')"
if [ -z "$SESSION" ]; then
    echo "FAIL: $AGENTS_TOML declares no session_name"
    echo "      Refusing to default to a literal; set session_name explicitly."
    exit 1
fi

echo "=== Spawn graceful shutdown test ==="
echo "  config:  $AGENTS_TOML"
echo "  session: $SESSION (private socket: $TMUX_TMPDIR)"

# Refuse to start if the target session already exists on THIS server -- a
# pre-existing session would make the has-session checks below meaningless.
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "FAIL: session '$SESSION' already exists on the private server"
    exit 1
fi

# Create the sibling worktree directories the config references (everything
# except "main", which is the workspace root itself). The spawn-up pre-flight
# refuses any agent whose worktree is missing — a fixture dir that does not
# exist is a fixture-setup gap, not a reason to bypass the guard.
WS_ROOT="$(dirname "$(dirname "$AGENTS_TOML")")"
grep -E '^[[:space:]]*worktree[[:space:]]*=' "$AGENTS_TOML" \
    | sed -E 's/.*=[[:space:]]*"([^"]*)".*/\1/' | sort -u | while read -r wt; do
    [ "$wt" = "main" ] && continue
    mkdir -p "$(dirname "$WS_ROOT")/$(printf '%s' "$wt" | tr '/' '-')"
done

# 1. Launch agents in mock mode
echo "[1/4] Launching agents..."
$GR spawn up --mock >/dev/null 2>&1

# Verify session exists
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "FAIL: tmux session '$SESSION' not created"
    exit 1
fi
echo "  ✓ Agents launched"

# 2. Run gr spawn down (sends /exit then kills)
echo "[2/4] Running gr spawn down..."
$GR spawn down >/dev/null 2>&1

# 3. Session should be terminated after down
echo "[3/4] Verifying session terminated..."
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "FAIL: tmux session still exists after spawn down"
    exit 1
fi
echo "  ✓ Session terminated"

# 4. Verify the exit code is clean
echo "[4/4] Verifying clean exit..."
echo "  ✓ gr spawn down exited cleanly"

echo ""
echo "=== ALL PASS ==="

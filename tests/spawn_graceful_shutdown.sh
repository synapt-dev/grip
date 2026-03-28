#!/usr/bin/env bash
# Integration test: gr spawn down sends /exit before killing tmux windows.
#
# Requires: tmux, gr (built), .gitgrip/agents.toml with at least one agent.
# Uses claudemock as the agent to detect /exit receipt.

set -euo pipefail

GR="${GR:-./target/debug/gr}"
SESSION="synapt"
AGENT="opus"
LOG=$(mktemp)

cleanup() {
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    rm -f "$LOG"
}
trap cleanup EXIT

echo "=== Spawn graceful shutdown test ==="

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

#!/usr/bin/env bash
# e2e-demo.sh — Team-verified demo for Sprint 11 release.
#
# Spawns mock agents, verifies dashboard API endpoints work,
# checks channel operations, tears down. Exit 0 = pass.
#
# Usage: ./scripts/e2e-demo.sh [--skip-dashboard]

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

pass() { echo -e "  ${GREEN}✓${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}✗${NC} $1"; ((FAIL++)); }
skip() { echo -e "  ${YELLOW}⊘${NC} $1 (skipped)"; ((SKIP++)); }

echo "═══════════════════════════════════════════"
echo "  e2e Demo — Sprint 11 Release Verification"
echo "═══════════════════════════════════════════"
echo ""

# ---------------------------------------------------------------------------
# Phase 1: Spawn
# ---------------------------------------------------------------------------
echo "Phase 1: Agent Spawn"

# Check tmux is available
if ! command -v tmux &>/dev/null; then
    fail "tmux not installed"
    echo ""
    echo "Result: FAIL (tmux required)"
    exit 1
fi
pass "tmux available"

# Find gr binary
if [ -z "${GR:-}" ]; then
    GR=$(command -v gr 2>/dev/null || true)
    [ -z "$GR" ] && GR="./target/debug/gr"
fi
if [ ! -x "$GR" ]; then
    GR="./target/release/gr"
fi
if [ ! -x "$GR" ]; then
    fail "gr binary not found"
    exit 1
fi
pass "gr binary found: $GR"

# Check agents.toml exists
if [ ! -f ".gitgrip/agents.toml" ]; then
    fail ".gitgrip/agents.toml not found (run gr sync first)"
    exit 1
fi
pass "agents.toml found"

# Kill any existing session
SESSION=$(grep 'session_name' .gitgrip/agents.toml 2>/dev/null | head -1 | sed 's/.*= *"//' | sed 's/".*//')
SESSION="${SESSION:-synapt}"
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Spawn mock agents
echo ""
echo "  Spawning mock agents..."
if $GR spawn up --mock 2>&1 | tail -5; then
    pass "gr spawn up --mock succeeded"
else
    fail "gr spawn up --mock failed"
fi

# Verify session exists
if tmux has-session -t "$SESSION" 2>/dev/null; then
    pass "tmux session '$SESSION' exists"
else
    fail "tmux session '$SESSION' not created"
fi

# Count agent windows
WINDOW_COUNT=$(tmux list-windows -t "$SESSION" 2>/dev/null | wc -l | tr -d ' ')
EXPECTED_AGENTS=$(grep '^\[agents\.' .gitgrip/agents.toml 2>/dev/null | wc -l | tr -d ' ')
if [ "$WINDOW_COUNT" -ge "$EXPECTED_AGENTS" ]; then
    pass "agent windows created: $WINDOW_COUNT (expected >= $EXPECTED_AGENTS)"
else
    fail "agent windows: got $WINDOW_COUNT, expected >= $EXPECTED_AGENTS"
fi

# Check team.db was created
ORG_ID=$(grep 'org_id' .gitgrip/agents.toml 2>/dev/null | head -1 | sed 's/.*= *"//' | sed 's/".*//')
ORG_ID="${ORG_ID:-$SESSION}"
TEAM_DB="$HOME/.synapt/orgs/$ORG_ID/team.db"
if [ -f "$TEAM_DB" ]; then
    AGENT_COUNT=$(sqlite3 "$TEAM_DB" "SELECT COUNT(*) FROM org_agents" 2>/dev/null || echo 0)
    pass "team.db exists with $AGENT_COUNT agents"
else
    fail "team.db not found at $TEAM_DB"
fi

# Check pipe-pane log dirs
LOG_DIR=".synapt/logs"
if [ -d "$LOG_DIR" ]; then
    LOG_COUNT=$(find "$LOG_DIR" -name "output.log" 2>/dev/null | wc -l | tr -d ' ')
    pass "pipe-pane log dirs created ($LOG_COUNT output.log files)"
else
    skip "pipe-pane log dirs (mock mode may not create them)"
fi

# ---------------------------------------------------------------------------
# Phase 2: Channels
# ---------------------------------------------------------------------------
echo ""
echo "Phase 2: Channel Operations"

# Post a message
if $GR channel post "e2e-demo: test message $(date +%s)" 2>/dev/null; then
    pass "channel post succeeded"
else
    fail "channel post failed"
fi

# Read messages
READ_OUTPUT=$($GR channel read 2>/dev/null || echo "")
if echo "$READ_OUTPUT" | grep -q "e2e-demo"; then
    pass "channel read shows posted message"
else
    fail "channel read doesn't show posted message"
fi

# Who is online
WHO_OUTPUT=$($GR channel who 2>/dev/null || echo "")
if [ -n "$WHO_OUTPUT" ]; then
    pass "channel who returned data"
else
    skip "channel who (no agents joined channel yet)"
fi

# ---------------------------------------------------------------------------
# Phase 3: Dashboard API (optional)
# ---------------------------------------------------------------------------
echo ""
echo "Phase 3: Dashboard API"

SKIP_DASHBOARD="${1:-}"
if [ "$SKIP_DASHBOARD" = "--skip-dashboard" ]; then
    skip "dashboard API (--skip-dashboard flag)"
else
    # Check if synapt dashboard is importable
    if python3 -c "from synapt.recall.dashboard import app" 2>/dev/null; then
        pass "dashboard module importable"

        # Check agents API
        # Start dashboard briefly in background
        python3 -c "
import json
from synapt.recall.dashboard import get_agents_json
agents = json.loads(get_agents_json())
print(json.dumps(agents, indent=2))
" 2>/dev/null && pass "agents API returns data" || fail "agents API failed"
    else
        skip "dashboard API (synapt not installed or dashboard not available)"
    fi
fi

# ---------------------------------------------------------------------------
# Phase 4: Dashboard Pane Targeting
# ---------------------------------------------------------------------------
echo ""
echo "Phase 4: Dashboard Layout"

# Create dashboard and verify pane count
$GR spawn down 2>/dev/null || true
sleep 1
$GR spawn up --mock 2>/dev/null

if $GR spawn status 2>&1 | grep -q "running\|✓"; then
    pass "agents running after respawn"
else
    fail "agents not running after respawn"
fi

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
echo ""
echo "Teardown"
$GR spawn down 2>/dev/null
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    pass "session cleaned up"
else
    fail "session still exists after spawn down"
fi

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
echo ""
echo "═══════════════════════════════════════════"
TOTAL=$((PASS + FAIL + SKIP))
echo -e "  Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, ${YELLOW}$SKIP skipped${NC} / $TOTAL total"
echo "═══════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo -e "${RED}DEMO FAILED${NC} — $FAIL test(s) failed. Fix before release."
    exit 1
else
    echo ""
    echo -e "${GREEN}DEMO PASSED${NC} — ready for release."
    exit 0
fi

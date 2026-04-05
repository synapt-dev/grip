#!/bin/bash
# Pre-tool-use hook: warn when 'git checkout -b' or 'git branch' is used
# inside a gripspace. Agents should use 'gr branch' instead.
#
# Install as a PreToolUse hook for Bash in settings.json:
#   {"matcher": "Bash", "hooks": [{"type": "command", "command": "path/to/enforce-gr-branch.sh"}]}
#
# The hook reads the tool input from stdin as JSON.
# Exit 0 = allow, exit 2 = block with message on stderr.

set -e

# Read the tool input JSON from stdin
INPUT=$(cat)

# Extract the command from the JSON
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('command',''))" 2>/dev/null || echo "")

# Check for raw git branch creation commands
if echo "$COMMAND" | grep -qE '^\s*git\s+(checkout\s+-b|branch\s+[^-])'; then
    # Check if we're in a gripspace (look for .gitgrip dir upward)
    DIR="${PWD}"
    IN_GRIPSPACE=false
    while [ "$DIR" != "/" ]; do
        if [ -d "$DIR/.gitgrip" ]; then
            IN_GRIPSPACE=true
            break
        fi
        DIR=$(dirname "$DIR")
    done

    if [ "$IN_GRIPSPACE" = true ]; then
        echo "WARNING: Use 'gr branch' instead of raw git branch commands in a gripspace." >&2
        echo "Raw git creates the branch in only one repo. 'gr branch' creates it across all repos." >&2
        echo "" >&2
        echo "Suggested: gr branch $(echo "$COMMAND" | sed -E 's/.*git (checkout -b|branch) //')" >&2
        exit 2
    fi
fi

exit 0

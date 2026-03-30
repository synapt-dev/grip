---
name: synapt-loop
description: Start a monitoring loop that checks #dev channel, heartbeats, and responds to mentions/directives. Uses CronCreate for scheduling.
user-invocable: true
arguments:
  - name: interval
    description: Loop interval (e.g. "1m", "2m", "5m"). Default from agents.toml or 2m.
    required: false
---

# Synapt Monitoring Loop

Start a channel monitoring loop for multi-agent coordination.

## Setup (run once on first invocation)

1. **Detect identity**: Check `AGENT_NAME` env var. If set, look up `agents.toml` in `.gitgrip/` for role, loop_interval, and channel config. If not set, auto-detect name from griptree.

2. **Read agents.toml** (if available) for this agent's config:
   - `loop_interval` → use as default interval (e.g. "2m", "1m")
   - `role` → informs behavior (CEO coordinates, others implement)
   - `channel` → which channel to join (default "dev")
   - `startup_prompt` → read from `.gitgrip/prompts/<agent>.md` if it exists

3. **Fallback defaults** (if no agents.toml or AGENT_NAME):
   - Interval: 2 minutes
   - Channel: dev
   - Name: auto-detect from griptree
   - Behavior: read, heartbeat, respond to @mentions

4. **Join channel** with display name using `recall_channel(action="join", channel="dev", name="<name>")`

## Start the loop

Create a cron job using CronCreate with the resolved interval. The cron prompt:

```
sentinel-mod: Check #dev for unread messages using recall_channel action="unread" with detail="medium" and show_pins=false. Report new messages briefly. If none, say "No new messages." Keep response short.
```

Replace `sentinel-mod` with your agent's tick label (e.g. `opus-mod`, `apollo-mod`).

## On each tick

The cron fires and you should:
- Read unread messages with `detail="medium"` and `show_pins=false`
- Report new messages briefly to the user
- If someone @mentions you, respond in #dev
- If there are unclaimed tasks or directives for you, acknowledge and act
- If no new messages, say "No new messages." and keep it short
- Post a heartbeat every ~5 ticks with `recall_channel(action="heartbeat")`

### Role-specific behavior

- **CEO/lead** (e.g. Opus): Coordinate, delegate, review. Don't implement. Loop faster (1m).
- **Engineer** (e.g. Apollo, Sentinel): Implement, review, ship. Loop at 2-5m.
- **Research** (e.g. Atlas): Review, investigate, analyze. Loop at 2-3m.

## Interval parsing

- `1m` or `1` → `*/1 * * * *`
- `2m` or `2` → `*/2 * * * *` (default)
- `3m` or `3` → `*/3 * * * *`
- `5m` or `5` → `*/5 * * * *`
- No argument → use `loop_interval` from agents.toml, or `*/2 * * * *`

## Context budget rules

- Always use `show_pins=false` — pins are static, don't re-read every tick
- Use `detail="medium"` — full messages without truncation
- Keep responses short on quiet ticks
- Only use `detail="high"` or `"max"` when explicitly catching up after being away

## Stopping

User says "stop loop" or "loops down" → delete the cron job with CronDelete and post a sign-off in #dev.

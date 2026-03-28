# gr spawn — Multi-Agent Session Manager

Launch and manage a team of AI agents from a single command. Each agent runs in its own tmux window with a dedicated role, worktree, and communication channel.

## Quick Start

```bash
# 1. Configure your team
vim .gitgrip/agents.toml

# 2. Launch in mock mode (safe — no real agents)
gr spawn --mock

# 3. Check status
gr spawn --status

# 4. Stop all agents
gr spawn --stop
```

## Prerequisites

- **tmux** installed and available in PATH
- **gitgrip** (`gr`) built and installed
- A `.gitgrip/agents.toml` config file in your gripspace (`.gitgrip/` directory)

## Configuration

### agents.toml

Create `.gitgrip/agents.toml` in your gripspace:

```toml
[spawn]
session_name = "myproject"    # tmux session name
channel = "dev"               # default communication channel
auto_journal = true           # agents read their journal on startup
mock_launch = true            # set false only when ready for real agents

[agents.lead]
role = "Tech lead — architecture, code review"
model = "claude-opus-4-6"
tool = "claude"
worktree = "main"
startup_prompt = ".gitgrip/prompts/lead.md"
loop_interval = "2m"
heartbeat_interval = 60
timeout_threshold = 180
restart_policy = "always"
max_restarts = 3
env = { AGENT_NAME = "lead", SYNAPT_ROLE = "lead" }
```

See the [field reference](#field-reference) below for all available options.

### Startup Prompts

Each agent can have a startup prompt file (Markdown) that defines its role, responsibilities, and boot sequence:

```
.gitgrip/
  agents.toml
  prompts/
    lead.md
    engineer.md
    reviewer.md
```

Prompts are injected via the `--prompt` flag when launching the agent's CLI tool. They should include:
- Role description
- Responsibilities
- Startup checklist (join channel, read journal, check unread, start loop)
- Team roster
- Lane-specific guidelines

## Commands

### Launch all agents

```bash
gr spawn                      # uses .gitgrip/agents.toml
gr spawn --config path/to/agents.toml  # custom config path
gr spawn --mock               # force mock mode (overrides config)
```

### Launch a single agent

```bash
gr spawn --agent opus         # launch only the "opus" agent
```

### Check status

```bash
gr spawn --status             # show tmux window state + heartbeat status
gr spawn --list               # show configured agents from agents.toml
```

### Stop agents

```bash
gr spawn --stop               # stop all agent windows
gr spawn --stop opus          # stop a single agent
```

## tmux Layout

Each agent gets its own tmux **window** (not pane) within the configured session:

```
Session: myproject
├── Window 0: "you"        (your terminal, untouched)
├── Window 1: "lead"       (agent)
├── Window 2: "engineer"   (agent)
└── Window 3: "reviewer"   (agent)
```

Switch between agents with `Ctrl-b` + window number, or:
```bash
tmux select-window -t myproject:lead
```

## Phased Rollout

`gr spawn` is designed for safe, incremental adoption:

| Phase | What happens | Config |
|-------|-------------|--------|
| **Phase 1 (mock)** | tmux windows created, env vars set, `echo` instead of real agents | `mock_launch = true` |
| **Phase 2 (single)** | One real agent launched for testing | `gr spawn --agent lead` with `mock_launch = false` |
| **Phase 3 (full)** | All agents launched | `mock_launch = false` |

**Always start with Phase 1.** Verify tmux windows, env vars, and the watcher before connecting real agents.

## Field Reference

### [spawn] section

| Field | Default | Description |
|-------|---------|-------------|
| `session_name` | `"synapt"` | tmux session name |
| `channel` | `"dev"` | Default channel all agents join |
| `auto_journal` | `true` | Agents read `recall_journal` on startup |
| `mock_launch` | `false` | Use echo/sleep instead of real agent launch |

### [agents.*] section

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `role` | yes | — | Human-readable role description |
| `model` | no | `claude-sonnet-4-6` | Model ID |
| `tool` | no | `claude` | CLI tool (`claude`, `codex`, `cursor`) |
| `worktree` | no | `main` | Git worktree or `"new"` to auto-create |
| `startup_prompt` | no | — | Path to .md startup prompt file |
| `channel` | no | from `[spawn]` | Channel to auto-join |
| `loop_interval` | no | `5m` | Channel read loop cadence |
| `heartbeat_interval` | no | `60` | Seconds between heartbeat pings |
| `timeout_threshold` | no | `180` | Seconds of silence before stale alert |
| `restart_policy` | no | `always` | `always`, `once`, or `never` |
| `restart_delay` | no | `5` | Seconds before restart (supports exponential backoff) |
| `max_restarts` | no | `3` | Max restart attempts per session |
| `env` | no | `{}` | Extra environment variables |

## Environment Variables

`gr spawn` injects these env vars into each agent's shell:

| Variable | Source | Example |
|----------|--------|---------|
| `AGENT_NAME` | TOML key or `env` table | `opus` |
| `AGENT_ROLE` | `role` field | `CEO — product design` |
| `GRIPTREE_NAME` | griptree directory name | `synapt-dev` |
| `SYNAPT_CHANNELS` | `channel` field | `dev` |
| `SYNAPT_LOOP_INTERVAL` | `loop_interval` field | `2m` |

Plus any custom vars from the `env` table in `agents.toml`.

## Monitoring

The spawn watcher (`spawn_watcher.py`) runs alongside `gr spawn` to monitor agent health:

- **Heartbeat detection**: alerts when an agent hasn't posted in `timeout_threshold` seconds
- **Process monitoring**: checks tmux pane PID status
- **Auto-recovery**: restarts crashed agents per `restart_policy`
- **Event logging**: writes restart events to `.synapt/recall/spawn_events.jsonl`

See the watcher's own documentation for setup details.

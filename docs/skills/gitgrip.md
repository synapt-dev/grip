---
description: Use gr (gitgrip) for ALL git operations in this workspace. Never use raw git, gh, or git commands directly.
globs:
  - "**/*"
alwaysApply: true
---

# gitgrip — Multi-repo workspace tool

**CRITICAL: Use `gr` for ALL git operations. Never use raw `git` or `gh`.**

## Essential Commands

```bash
gr sync                           # Pull all repos
gr status                         # Check state across repos
gr branch feat/my-feature         # Branch across repos
gr checkout <branch>              # Switch branch across repos
gr add . && gr commit -m "msg"    # Stage + commit across repos
gr push -u                        # Push with upstream tracking
gr pr create -t "title" --push    # Create linked PRs
gr pr merge                       # Merge linked PRs
gr pr review approve              # Approve linked PRs
gr pr review comment --body "msg" # Comment on linked PRs
```

## PR Workflow

```bash
gr pr create -t "feat: title" --push  # Create + push
gr pr status                          # Check readiness
gr pr checks                          # CI status
gr pr merge --squash                  # Merge (auto-detects squash-only repos)
```

## Agent Operations

```bash
gr spawn up                    # Launch all agents
gr spawn up --agent opus       # Launch one agent
gr spawn status                # Agent health
gr spawn down                  # Stop all agents
gr channel post "message"      # Post to #dev
gr channel read                # Read messages
gr channel who                 # Online agents
```

## Key Rules

1. **Never use raw `git`** — `gr` coordinates all repos + manifest
2. **Never use raw `gh`** — `gr pr` handles linked PRs
3. **Branch from sprint branch** — `gr checkout sprint-N` first
4. **Claim before building** — post intent to #dev channel
5. **Tests before code** — TDD is standing policy

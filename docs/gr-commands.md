# gr Command Reference

Quick reference for all `gr` commands. Run `gr <command> --help` for full options.

## Core Git Operations

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `gr init <url>` | Initialize workspace from manifest URL | `--from-dirs`, `--from-repo` |
| `gr sync` | Pull all repos in parallel | `--sequential`, `--group`, `--repo` |
| `gr status` | Show status across all repos | `--json` |
| `gr branch <name>` | Create branch across repos | `--repo`, `--delete`, `--move` |
| `gr checkout <branch>` | Switch branch across repos | `-b` (create), `--repo`, `--base` |
| `gr add` | Stage changes across repos | |
| `gr restore` | Unstage/discard changes | `--staged`, `--repo` |
| `gr diff` | Show diff across repos | |
| `gr commit -m "msg"` | Commit across repos | |
| `gr push` | Push across repos | `-u` (upstream) |
| `gr pull` | Pull across repos | `--rebase` |
| `gr rebase` | Rebase across repos | `--upstream`, `--abort`, `--continue` |
| `gr cherry-pick` | Cherry-pick across repos | `--abort`, `--continue` |
| `gr prune` | Clean up merged branches | `--execute`, `--remote` |
| `gr gc` | Garbage collection | `--aggressive`, `--dry-run` |
| `gr grep <pattern>` | Search across repos | `-i`, `--parallel` |
| `gr forall -c "cmd"` | Run command in each repo | |

## Pull Request Operations

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `gr pr create` | Create linked PRs | `-t` (title), `--push`, `--draft`, `--repo` |
| `gr pr status` | Show PR readiness | |
| `gr pr merge` | Merge linked PRs | `--method`, `--squash`, `--force`, `--auto`, `--wait` |
| `gr pr review <event>` | Review PRs (approve/request-changes/comment) | `--body` |
| `gr pr edit` | Update title/body | `--title`, `--body` |
| `gr pr checks` | Show CI status | `--repo` |
| `gr pr diff` | Show PR diff | `--stat` |
| `gr pr list` | List PRs | `--state`, `--repo`, `--limit` |
| `gr pr view` | View PR details | `--repo` |

## Workspace Management

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `gr repo add/list/remove` | Manage repos in manifest | |
| `gr group list/add/remove` | Manage repo groups | |
| `gr target list/set/unset` | PR target branch | `--repo` |
| `gr link` | Manage copyfile/linkfile | |
| `gr manifest import/sync/schema` | Manifest operations | |
| `gr run` | Execute workspace scripts | |
| `gr env` | Show workspace env vars | |
| `gr bench` | Run benchmarks | |
| `gr verify` | Verify workspace assertions | |

## Griptrees (Multi-Branch Workspaces)

| Command | Description |
|---------|-------------|
| `gr tree add <branch>` | Create parallel workspace |
| `gr tree list` | List all griptrees |
| `gr tree remove <branch>` | Remove griptree |
| `gr tree lock <branch>` | Lock griptree |

## Agent & Channel Operations

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `gr spawn up [agent]` | Launch agent sessions in tmux | |
| `gr spawn down [agent]` | Stop agent sessions | |
| `gr spawn status` | Show agent status | |
| `gr spawn attach <agent>` | Attach to agent's tmux pane | |
| `gr spawn logs <agent>` | View agent output | `--all` |
| `gr spawn dashboard` | Mission control 2x2 grid | |
| `gr channel post "msg"` | Post to #dev | `--channel`, `--pin` |
| `gr channel read` | Read recent messages | `--limit`, `--detail` |
| `gr channel who` | Show online agents | |
| `gr channel search "q"` | Search channel history | `--channel` |
| `gr channel list` | List channels | |
| `gr channel join` | Join a channel | `--name` |
| `gr agent context` | Show agent context | |
| `gr agent build` | Build agent artifacts | |
| `gr agent test` | Run agent tests | |
| `gr agent verify` | Verify agent setup | |

## Platform Operations

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `gr issue list` | List issues | `--state`, `--label`, `--limit` |
| `gr issue create` | Create issue | `-t` (title), `-b` (body), `-l` (label) |
| `gr issue view <n>` | View issue | `--repo` |
| `gr issue close <n>` | Close issue | `--repo` |
| `gr issue reopen <n>` | Reopen issue | `--repo` |
| `gr ci run/list/status` | CI/CD operations | |
| `gr release` | Automated release | |
| `gr mcp server` | Start MCP server | |
| `gr completions <shell>` | Generate completions | bash, zsh, fish |

## Common Patterns

```bash
# Standard feature workflow
gr sync && gr branch feat/my-feature
# ... make changes ...
gr add . && gr commit -m "feat: description" && gr push -u
gr pr create -t "feat: description" --push

# Single-repo operations
gr checkout main --repo synapt          # Switch one repo
gr branch feat/x --repo gitgrip         # Branch in one repo

# Sprint branch workflow
gr target set sprint-5/week-1           # PRs target sprint branch
gr pr create -t "feat: title"           # PR targets sprint branch
gr target set main                      # Reset after sprint

# Review workflow
gr pr review approve                    # Approve linked PRs
gr pr review comment --body "LGTM"      # Comment on linked PRs
gr pr review request-changes --body "Fix X"  # Request changes
```

## Global Flags

All commands support: `--quiet` (`-q`), `--verbose` (`-v`), `--json`, `--help` (`-h`)

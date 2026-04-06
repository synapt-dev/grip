# Contributing to gitgrip

Thank you for your interest in contributing to gitgrip! This document provides guidelines for contributing.

## Getting Started

### Prerequisites
- Rust toolchain (rustc, cargo) — MSRV 1.80
- Git

### Setting Up the Development Environment

```bash
# Clone the repository
git clone git@github.com:laynepenney/grip.git
cd gitgrip

# Build the project
cargo build

# Run tests
cargo test
```

## Development Workflow

### 1. Create a Feature Branch
Always create a new branch for your changes. Never work directly on `main`.

```bash
# Ensure you're on latest main
git checkout main
git pull origin main

# Create your feature branch
git checkout -b feat/your-feature-name
# or
git checkout -b fix/your-bugfix-name
# or
git checkout -b docs/your-docs-change-name
```

### 2. Make Changes
Make your changes to the codebase. Follow Rust conventions:

- Use `cargo clippy` to check for linting issues
- Use `cargo fmt` to format code
- Add tests for new functionality
- Update documentation as needed

### 3. Commit Your Changes
Write descriptive commit messages following [conventional commits](https://www.conventionalcommits.org/):

```bash
# Format your code first
cargo fmt

# Run linting
cargo clippy

# Stage and commit
git add <files>
git commit -m "feat: add new command for xyz"

# Or for fixes:
git commit -m "fix: resolve issue with xyz"

# Or for documentation:
git commit -m "docs: improve readme section"
```

### 4. Push Your Branch and Create a PR
```bash
git push origin feat/your-feature-name
gh pr create --title "feat: your feature description" --body "..."
```

### 5. Code Review
- Wait for CI checks to pass
- Address any review feedback
- Don't merge until all checks pass

### 6. Merge
Once approved and CI passes, merge the PR via GitHub's interface.

## Pull Request Guidelines

### What to Include
- Clear title describing the change
- Detailed description of the problem and solution
- Steps to test the changes
- Screenshots or GIFs for UI changes
- References to related issues

### Testing
- Run `cargo test` before pushing
- Add unit tests for new functionality
- Integration tests for user-facing commands

### Code Quality
- Run `cargo fmt` before committing
- Fix any `cargo clippy` warnings
- Document public APIs with doc comments

## Git Workflow

### Branch Strategy

**Main Branch (`main`)**
- Production-ready code only
- Protected with PR requirements and CI checks
- All PRs must target `main`
- Never force push to `main` ❌

**Feature Branches**
- All development happens here
- Short-lived, deleted after merge
- Clean merge history when merged properly

### Merge vs Rebase

**✅ Use REBASE** (correct):
```bash
git rebase origin/main           # Keeps history linear
git push --force-with-lease      # Safe force-push after rebase (feature branches only!)
```

**❌ DO NOT Use MERGE** (incorrect):
```bash
git merge origin/main            # Creates unnecessary merge commits
```

**❌ DO NOT Force Push to Main** (NEVER!):
```bash
git push --force origin main     # ABSOLUTELY FORBIDDEN
```

### Why Force Pushing to Main is Forbidden

1. **Destroys history**: Rewrites shared history, breaking anyone who pulled
2. **Lose work**: Others' commits may be erased
3. **Breaks CI/CD**: GitHub Actions and deploys may fail
4. **Trust issues**: Team members can't trust what they pulled

### Correct PR Process

1. Create feature branch from latest main
2. Make changes and commit
3. Push branch and create PR
4. Get reviewed and approved
5. Merge via GitHub button
6. Delete feature branch

### If You Accidentally Force Pushed to Main

1. **Stop immediately** - Don't do anything else
2. **Contact team** - Alert everyone who might have pulled
3. **Restore from backup** - Use reflog or team members' clones
4. **Redo properly** - Cherry-pick commits to a new branch and create proper PR

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Questions?

Open an issue for discussion or reach out to the maintainers.

## Git Workflow for the Workspace

This section applies when using gitgrip to manage the workspace itself.

### Creating Changes in the Workspace

When working with the gitgrip workspace:

```bash
# Make changes in the tooling repo (where gitgrip source lives)
cd tooling/src/cli/commands/repo.rs
# ... edit file ...

# Stage and commit (runs gr commit across all workspaces)
git add .
git commit -m "fix: ..."

# Push (runs gr push across all workspaces)
git push
```

### Creating a PR for gitgrip Itself

1. Make changes to gitgrip code in the `tooling` repo
2. The commit/push operations automatically apply to all workspace repos
3. When creating a PR, only ONE GitHub PR is needed (for the tooling repo)
4. GitHub Actions and CI run from the tooling repo
5. Merging the PR updates only the tooling repo's history

### Important: PR for gitgrip Goes to the gitgrip Repo

The PR for gitgrip changes should be created from the `tooling` repo's perspective:
```bash
gh pr create  # From the tooling directory
```
This creates the PR for `github.com/laynepenney/grip`, not the workspace manifest.

## Documentation

- **CLAUDE.md** - Development guide and command reference
- **README.md** - User-facing documentation
- **CONTRIBUTING.md** - This file
- **CHANGELOG.md** - Version history
- **docs/SKILL.md** - AI assistant skill definition
- **docs/MANIFEST.md** - Manifest reference
- **.claude/skills/gitgrip/SKILL.md** - Claude Code skill integration


## Worktree Conflicts

If you see an error like:
```
fatal: 'main' is already used by worktree at '...'
```

This happens when gitgrip has multiple worktrees (e.g., in codi-workspace and codi-dev).

**To resolve:**
1. Create a new branch instead of checking out main:
   ```bash
   git checkout -b fix/my-feature
   gr branch fix/my-feature
   ```

2. Or use the existing worktree at codi-workspace for gitgrip-related work

**Prevention:**
- Keep main checked out in one workspace (codi-workspace recommended)
- Use other workspaces for feature branches
- Or use `gr branch` which handles this automatically

# gr pr create diagnostics fix

**Issue**: grip#722
**Date**: 2026-05-08
**Author**: Apollo
**Status**: Design memo (pre-implementation)

## Problem

`gr pr create` has three related diagnostic failures that cost 5-10 minutes per occurrence and require fallback to `gh pr create --repo`. Observed independently by 3 agents across 4 PRs in a single substrate-day.

## Root cause analysis

### 1. Permissive `has_commits_ahead()` (create.rs:365-406)

```rust
// Line 386: neither remote nor local base ref exists
Err(_) => return Ok(true),
```

When the target branch doesn't exist anywhere, the function returns `Ok(true)` (assume changes exist). This means repos with nonexistent target branches pass the pre-check and reach the GitHub API, which returns a raw 422 error.

**Fix**: Return a new `BranchNotFound` result instead of silently assuming success. The caller should surface "base branch '{name}' not found on remote or locally" before attempting the API call.

### 2. Catch-all `PlatformError::ApiError` (github.rs:202)

```rust
result.map_err(|e| PlatformError::ApiError(format!("Failed to create PR: {}", e)))?
```

All octocrab errors become a single string. GitHub's 422 responses contain structured JSON with specific error messages ("Head sha can't be null", "No commits between {base} and {head}", "Validation Failed") that should map to specific error variants.

**Fix**: Parse the octocrab error for known GitHub API patterns and map to specific `PlatformError` variants.

### 3. No `BranchNotFound` in `PlatformError` (traits.rs:10-35)

The enum has `NotFound(String)` but it's generic. Branch-specific not-found needs its own variant with the branch name and direction (head vs base) for actionable diagnostics.

## Proposed changes

### traits.rs: Add error variants

```rust
#[derive(Error, Debug)]
pub enum PlatformError {
    // ... existing variants ...

    #[error("Branch not found: {0}")]
    BranchNotFound(String),

    #[error("Base branch '{base}' does not exist on remote for {repo}")]
    BaseBranchNotFound { repo: String, base: String },

    #[error("Head branch '{head}' does not exist on remote for {repo}")]
    HeadBranchNotFound { repo: String, head: String },
}
```

### create.rs: Pre-check branch existence

Before calling `platform.create_pull_request()`, add a branch existence check:

```rust
// Before line 227
match platform.check_branch_exists(&repo.owner, &repo.repo, repo.target_branch()).await {
    Ok(true) => {},
    Ok(false) => {
        spinner.finish_with_message(format!(
            "{}: skipped - base branch '{}' does not exist on remote. \
             Available branches: use 'gh api repos/{}/{}/branches --jq .[].name' to list.",
            repo.name, repo.target_branch(), repo.owner, repo.repo
        ));
        all_failed_repos.push((
            repo.name.clone(),
            format!("base branch '{}' not found on remote", repo.target_branch()),
        ));
        continue;
    },
    Err(e) => {
        // Network error checking branch; proceed to API call (fail there if needed)
        Output::warning(&format!("{}: could not verify base branch: {}", repo.name, e));
    }
}
```

### github.rs: Parse 422 responses

```rust
async fn create_pull_request(...) -> Result<PRCreateResult, PlatformError> {
    // ... existing code ...
    let pr = result.map_err(|e| {
        let msg = e.to_string();
        if msg.contains("Validation Failed") {
            if msg.contains("head sha") || msg.contains("Head sha") {
                PlatformError::HeadBranchNotFound {
                    repo: repo.to_string(),
                    head: head.to_string(),
                }
            } else if msg.contains("No commits between") {
                PlatformError::ApiError(format!(
                    "No commits between '{}' and '{}' in {}/{}",
                    base, head, owner, repo
                ))
            } else {
                PlatformError::ApiError(format!("GitHub validation error for {}/{}: {}", owner, repo, msg))
            }
        } else {
            PlatformError::ApiError(format!("Failed to create PR in {}/{}: {}", owner, repo, msg))
        }
    })?;
    // ...
}
```

### has_commits_ahead: Return structured result

```rust
pub(crate) fn has_commits_ahead(
    repo: &Repository,
    branch: &str,
    base: &str,
) -> anyhow::Result<bool> {
    // ... existing code ...
    let base_branch = match repo.find_reference(&base_ref) {
        Ok(r) => r,
        Err(_) => {
            match repo.find_reference(&format!("refs/heads/{}", base)) {
                Ok(r) => r,
                Err(_) => {
                    // Instead of silently assuming true, warn and return true
                    // The pre-check in create_pull_request will catch this
                    tracing::warn!(
                        branch = branch,
                        base = base,
                        "Base branch not found locally or on remote; \
                         will verify via API before PR creation"
                    );
                    return Ok(true);
                }
            }
        }
    };
    // ...
}
```

### New trait method: check_branch_exists

Add to `HostingPlatform` trait:

```rust
async fn check_branch_exists(
    &self,
    owner: &str,
    repo: &str,
    branch: &str,
) -> Result<bool, PlatformError>;
```

GitHub implementation:

```rust
async fn check_branch_exists(&self, owner: &str, repo: &str, branch: &str)
    -> Result<bool, PlatformError>
{
    let client = self.get_client().await?;
    match client.repos(owner, repo).get_ref(
        &octocrab::params::repos::Reference::Branch(branch.to_string())
    ).await {
        Ok(_) => Ok(true),
        Err(octocrab::Error::GitHub { source, .. }) if source.status_code == 404 => Ok(false),
        Err(e) => Err(PlatformError::ApiError(format!("Failed to check branch: {}", e))),
    }
}
```

## Scope assessment

**Small-medium scope**. Three files touched (traits.rs, github.rs, create.rs), one new trait method, error variant additions. No architectural changes.

**Risk**: Adding `check_branch_exists` to the `HostingPlatform` trait requires stub implementations in gitlab.rs, azure.rs, bitbucket.rs. These can return `Ok(true)` (optimistic, fall through to API error) to avoid blocking.

**Estimated effort**: 2-3 hours implementation + tests.

## Test plan

1. Unit test: `has_commits_ahead` with missing base branch logs warning
2. Unit test: `PlatformError` display for new variants
3. Integration test: `gr pr create` against nonexistent base branch produces actionable error
4. Integration test: `gr pr create --repo config` stays scoped to config repo
5. Regression: existing PR creation flow unchanged when branches exist

## Decision

Ship if implementation stays within the 3-file, 2-3 hour scope. If scope grows (e.g., octocrab error parsing is more complex than expected), land the memo + issue and queue for next sprint.

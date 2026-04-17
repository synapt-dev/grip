//! Git remote operations

use git2::Repository;
use std::process::Command;

use super::cache::invalidate_status_cache;
use super::{get_current_branch, GitError};
use crate::util::log_cmd;

#[cfg(feature = "telemetry")]
use crate::telemetry::metrics::GLOBAL_METRICS;
#[cfg(feature = "telemetry")]
use std::time::Instant;
#[cfg(feature = "telemetry")]
use tracing::{debug, instrument};

#[derive(Debug, Clone, Copy)]
pub enum PullMode {
    Merge,
    Rebase,
}

/// Get the URL of a remote
pub fn get_remote_url(repo: &Repository, remote: &str) -> Result<Option<String>, GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["remote", "get-url", remote])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if output.status.success() {
        let url = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Ok(Some(url))
    } else {
        Ok(None)
    }
}

/// Set the URL of a remote (creates if it doesn't exist)
pub fn set_remote_url(repo: &Repository, remote: &str, url: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    if get_remote_url(repo, remote)?.is_none() {
        let mut cmd = Command::new("git");
        cmd.args(["remote", "add", remote, url])
            .current_dir(repo_path);
        log_cmd(&cmd);
        cmd.output()
            .map_err(|e| GitError::OperationFailed(e.to_string()))?;
    } else {
        let mut cmd = Command::new("git");
        cmd.args(["remote", "set-url", remote, url])
            .current_dir(repo_path);
        log_cmd(&cmd);
        cmd.output()
            .map_err(|e| GitError::OperationFailed(e.to_string()))?;
    }
    Ok(())
}

/// Fetch from remote
#[cfg_attr(feature = "telemetry", instrument(skip(repo), fields(remote, success)))]
pub fn fetch_remote(repo: &Repository, remote: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    #[cfg(feature = "telemetry")]
    let start = Instant::now();

    let mut cmd = Command::new("git");
    cmd.args(["fetch", remote]).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let success = output.status.success();

    #[cfg(feature = "telemetry")]
    {
        let duration = start.elapsed();
        GLOBAL_METRICS.record_git("fetch", duration, success);
        debug!(
            remote,
            success,
            duration_ms = duration.as_millis() as u64,
            "Git fetch complete"
        );
    }

    if !success {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(interpret_push_error(&stderr)));
    }

    Ok(())
}

/// Pull latest changes (fetch + merge)
#[cfg_attr(feature = "telemetry", instrument(skip(repo), fields(remote, success)))]
pub fn pull_latest(repo: &Repository, remote: &str) -> Result<(), GitError> {
    pull_latest_with_mode(repo, remote, PullMode::Merge)
}

/// Pull latest changes with rebase
#[cfg_attr(feature = "telemetry", instrument(skip(repo), fields(remote, success)))]
pub fn pull_latest_rebase(repo: &Repository, remote: &str) -> Result<(), GitError> {
    pull_latest_with_mode(repo, remote, PullMode::Rebase)
}

fn pull_latest_with_mode(repo: &Repository, remote: &str, mode: PullMode) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);
    let has_upstream = get_upstream_branch(repo, None)?.is_some();

    #[cfg(feature = "telemetry")]
    let start = Instant::now();

    let mut cmd = Command::new("git");
    cmd.arg("pull");
    if matches!(mode, PullMode::Rebase) {
        cmd.arg("--rebase");
    }
    if !has_upstream {
        cmd.arg(remote);
    }
    cmd.current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let success = output.status.success();

    #[cfg(feature = "telemetry")]
    {
        let duration = start.elapsed();
        GLOBAL_METRICS.record_git("pull", duration, success);
        debug!(
            remote,
            success,
            duration_ms = duration.as_millis() as u64,
            "Git pull complete"
        );
    }

    if !success {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("CONFLICT") {
            return Err(GitError::OperationFailed(
                "Merge conflict occurred. Resolve conflicts manually.".to_string(),
            ));
        }
        if stderr.contains("non-fast-forward") {
            return Err(GitError::OperationFailed(
                "Non-fast-forward merge required. Please merge manually.".to_string(),
            ));
        }
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    // Invalidate cache
    invalidate_status_cache(&repo_path.to_path_buf());

    Ok(())
}

/// Pull latest changes from a specific upstream ref (e.g., origin/main)
#[cfg_attr(
    feature = "telemetry",
    instrument(skip(repo), fields(upstream, success))
)]
pub fn pull_latest_from_upstream(repo: &Repository, upstream: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);
    let (remote, branch) = split_upstream_ref(upstream)?;

    #[cfg(feature = "telemetry")]
    let start = Instant::now();

    let mut cmd = Command::new("git");
    cmd.args(["pull", &remote, &branch]).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let success = output.status.success();

    #[cfg(feature = "telemetry")]
    {
        let duration = start.elapsed();
        GLOBAL_METRICS.record_git("pull", duration, success);
        debug!(
            upstream,
            success,
            duration_ms = duration.as_millis() as u64,
            "Git pull complete"
        );
    }

    if !success {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("CONFLICT") {
            return Err(GitError::OperationFailed(
                "Merge conflict occurred. Resolve conflicts manually.".to_string(),
            ));
        }
        if stderr.contains("non-fast-forward") {
            return Err(GitError::OperationFailed(
                "Non-fast-forward merge required. Please merge manually.".to_string(),
            ));
        }
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    // Invalidate cache
    invalidate_status_cache(&repo_path.to_path_buf());

    Ok(())
}

fn split_upstream_ref(upstream: &str) -> Result<(String, String), GitError> {
    let (remote, branch) = upstream.split_once('/').ok_or_else(|| {
        GitError::OperationFailed(format!(
            "Invalid upstream ref '{}'. Expected '<remote>/<branch>'.",
            upstream
        ))
    })?;
    if remote.is_empty() || branch.is_empty() {
        return Err(GitError::OperationFailed(format!(
            "Invalid upstream ref '{}'. Expected '<remote>/<branch>'.",
            upstream
        )));
    }
    Ok((remote.to_string(), branch.to_string()))
}

/// Push branch to remote
#[cfg_attr(
    feature = "telemetry",
    instrument(skip(repo), fields(branch_name, remote, set_upstream, success))
)]
pub fn push_branch(
    repo: &Repository,
    branch_name: &str,
    remote: &str,
    set_upstream: bool,
) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    #[cfg(feature = "telemetry")]
    let start = Instant::now();

    let mut args = vec!["push", remote, branch_name];
    if set_upstream {
        args.insert(1, "-u");
    }

    let mut cmd = Command::new("git");
    cmd.args(&args).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    let success = output.status.success();

    #[cfg(feature = "telemetry")]
    {
        let duration = start.elapsed();
        GLOBAL_METRICS.record_git("push", duration, success);
        debug!(
            branch_name,
            remote,
            set_upstream,
            success,
            duration_ms = duration.as_millis() as u64,
            "Git push complete"
        );
    }

    if !success {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(interpret_push_error(&stderr)));
    }

    Ok(())
}

/// Interpret common git push/fetch errors into user-friendly messages
fn interpret_push_error(stderr: &str) -> String {
    let lower = stderr.to_lowercase();
    if lower.contains("non-fast-forward") {
        return format!(
            "Push rejected: remote has changes. Pull first with `gr sync`, then try again.\n\
             (Original: {})",
            stderr.trim()
        );
    }
    if lower.contains("could not read from remote") || lower.contains("repository not found") {
        return format!(
            "Cannot reach remote. Check your network connection and repository URL.\n\
             (Original: {})",
            stderr.trim()
        );
    }
    if lower.contains("permission denied") || lower.contains("authentication failed") {
        return format!(
            "Authentication failed. Run `gh auth login` to refresh credentials.\n\
             (Original: {})",
            stderr.trim()
        );
    }
    stderr.to_string()
}

/// Force push branch to remote
pub fn force_push_branch(
    repo: &Repository,
    branch_name: &str,
    remote: &str,
) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["push", "--force", remote, branch_name])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(interpret_push_error(&stderr)));
    }

    Ok(())
}

/// Delete a remote branch
pub fn delete_remote_branch(
    repo: &Repository,
    branch_name: &str,
    remote: &str,
) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["push", remote, "--delete", branch_name])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Get upstream tracking branch name
pub fn get_upstream_branch(
    repo: &Repository,
    branch_name: Option<&str>,
) -> Result<Option<String>, GitError> {
    let repo_path = super::get_workdir(repo);

    let branch = match branch_name {
        Some(name) => name.to_string(),
        None => get_current_branch(repo)?,
    };

    let mut cmd = Command::new("git");
    cmd.args([
        "rev-parse",
        "--abbrev-ref",
        &format!("{}@{{upstream}}", branch),
    ])
    .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if output.status.success() {
        let upstream = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Ok(Some(upstream))
    } else {
        Ok(None)
    }
}

/// Check if upstream branch exists on remote
pub fn upstream_branch_exists(repo: &Repository, remote: &str) -> Result<bool, GitError> {
    let upstream = get_upstream_branch(repo, None)?;
    match upstream {
        Some(name) => {
            let branch_name = name.split('/').next_back().unwrap_or(&name);
            Ok(super::branch::remote_branch_exists(
                repo,
                branch_name,
                remote,
            ))
        }
        None => Ok(false),
    }
}

/// Set upstream tracking for the current branch
pub fn set_upstream_branch(repo: &Repository, remote: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);
    let branch_name = get_current_branch(repo)?;

    let mut cmd = Command::new("git");
    cmd.args([
        "branch",
        "--set-upstream-to",
        &format!("{}/{}", remote, branch_name),
    ])
    .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Set upstream tracking for a specific local branch.
pub fn set_branch_upstream_ref(
    repo: &Repository,
    branch_name: &str,
    upstream: &str,
) -> Result<(), GitError> {
    split_upstream_ref(upstream)?;

    let repo_path = super::get_workdir(repo);
    let mut cmd = Command::new("git");
    cmd.args(["branch", "--set-upstream-to", upstream, branch_name])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    Ok(())
}

/// Hard reset to a target
pub fn reset_hard(repo: &Repository, target: &str) -> Result<(), GitError> {
    let repo_path = super::get_workdir(repo);

    let mut cmd = Command::new("git");
    cmd.args(["reset", "--hard", target]).current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(stderr.to_string()));
    }

    // Invalidate cache
    invalidate_status_cache(&repo_path.to_path_buf());

    Ok(())
}

/// Safe pull that handles deleted upstream branches
pub fn safe_pull_latest(
    repo: &Repository,
    default_branch: &str,
    remote: &str,
) -> Result<SafePullResult, GitError> {
    safe_pull_latest_with_mode(repo, default_branch, remote, PullMode::Merge)
}

pub fn safe_pull_latest_with_mode(
    repo: &Repository,
    default_branch: &str,
    remote: &str,
    mode: PullMode,
) -> Result<SafePullResult, GitError> {
    let current_branch = get_current_branch(repo)?;

    // If on default branch, just pull
    if current_branch == default_branch {
        let pull_result = match mode {
            PullMode::Merge => pull_latest(repo, remote),
            PullMode::Rebase => pull_latest_rebase(repo, remote),
        };
        return match pull_result {
            Ok(()) => Ok(SafePullResult {
                pulled: true,
                recovered: false,
                message: None,
            }),
            Err(e) => Ok(SafePullResult {
                pulled: false,
                recovered: false,
                message: Some(e.to_string()),
            }),
        };
    }

    // Check if upstream exists
    let has_upstream = get_upstream_branch(repo, None)?.is_some();
    let upstream_exists = upstream_branch_exists(repo, remote)?;

    if !upstream_exists {
        if !has_upstream {
            // No upstream configured - just fetch to update refs
            // This is normal for local branches that haven't been pushed yet
            fetch_remote(repo, remote)?;
            return Ok(SafePullResult {
                pulled: true, // Fetch succeeded, consider it a success
                recovered: false,
                message: Some("fetched (no upstream)".to_string()),
            });
        }

        // Check for local-only commits
        let has_local_commits = super::branch::has_commits_ahead(repo, default_branch)?;
        if has_local_commits {
            return Ok(SafePullResult {
                pulled: false,
                recovered: false,
                message: Some(format!(
                    "Branch '{}' has local commits not in '{}'. Push your changes or merge manually.",
                    current_branch, default_branch
                )),
            });
        }

        // Safe to switch - upstream was deleted and no local work would be lost
        super::branch::checkout_branch(repo, default_branch)?;
        match mode {
            PullMode::Merge => pull_latest(repo, remote)?,
            PullMode::Rebase => pull_latest_rebase(repo, remote)?,
        }

        return Ok(SafePullResult {
            pulled: true,
            recovered: true,
            message: Some(format!(
                "Switched from '{}' to '{}' (upstream branch was deleted)",
                current_branch, default_branch
            )),
        });
    }

    // Normal pull
    let pull_result = match mode {
        PullMode::Merge => pull_latest(repo, remote),
        PullMode::Rebase => pull_latest_rebase(repo, remote),
    };
    match pull_result {
        Ok(()) => Ok(SafePullResult {
            pulled: true,
            recovered: false,
            message: None,
        }),
        Err(e) => Ok(SafePullResult {
            pulled: false,
            recovered: false,
            message: Some(e.to_string()),
        }),
    }
}

/// Result of safe_pull_latest
#[derive(Debug, Clone)]
pub struct SafePullResult {
    /// Whether pull succeeded
    pub pulled: bool,
    /// Whether recovery was needed (switched to default branch)
    pub recovered: bool,
    /// Optional message
    pub message: Option<String>,
}

/// Ensure a named remote is configured in a git repository.
///
/// If `remote_name` is "origin", this is a no-op (origin is always set up by clone).
/// Otherwise, checks if the remote exists in the repo's git config. If not, looks up
/// the remote in `manifest_remotes` and adds it with URL = `base_fetch_url / repo_name.git`.
pub fn ensure_remote_configured(
    repo_path: &std::path::Path,
    remote_name: &str,
    repo_name: &str,
    manifest_remotes: Option<
        &std::collections::HashMap<String, crate::core::manifest::RemoteConfig>,
    >,
) -> Result<(), GitError> {
    if remote_name == "origin" {
        return Ok(());
    }

    // Check if remote already exists
    let mut cmd = Command::new("git");
    cmd.args(["remote", "get-url", remote_name])
        .current_dir(repo_path);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if output.status.success() {
        // Remote already exists
        return Ok(());
    }

    // Look up in manifest remotes and add it
    if let Some(rc) = manifest_remotes.and_then(|m| m.get(remote_name)) {
        let base = rc.fetch.trim_end_matches('/');
        let url = format!("{}/{}.git", base, repo_name);

        let mut cmd = Command::new("git");
        cmd.args(["remote", "add", remote_name, &url])
            .current_dir(repo_path);
        log_cmd(&cmd);
        let output = cmd
            .output()
            .map_err(|e| GitError::OperationFailed(e.to_string()))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(GitError::OperationFailed(format!(
                "Failed to add remote '{}': {}",
                remote_name,
                stderr.trim()
            )));
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::git::open_repo;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    fn setup_test_repo() -> (TempDir, Repository) {
        let temp = TempDir::new().unwrap();

        Command::new("git")
            .args(["init"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        Command::new("git")
            .args(["config", "user.name", "Test User"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Create initial commit
        fs::write(temp.path().join("README.md"), "# Test").unwrap();
        Command::new("git")
            .args(["add", "README.md"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Initial commit"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let repo = open_repo(temp.path()).unwrap();
        (temp, repo)
    }

    #[test]
    fn test_get_remote_url() {
        let (temp, repo) = setup_test_repo();

        // No remote yet
        assert!(get_remote_url(&repo, "origin").unwrap().is_none());

        // Add remote
        Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/test/repo.git",
            ])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let url = get_remote_url(&repo, "origin").unwrap();
        assert_eq!(url, Some("https://github.com/test/repo.git".to_string()));
    }

    #[test]
    fn test_set_remote_url() {
        let (_temp, repo) = setup_test_repo();

        // Create new remote
        set_remote_url(&repo, "origin", "https://github.com/test/repo1.git").unwrap();
        assert_eq!(
            get_remote_url(&repo, "origin").unwrap(),
            Some("https://github.com/test/repo1.git".to_string())
        );

        // Update remote
        set_remote_url(&repo, "origin", "https://github.com/test/repo2.git").unwrap();
        assert_eq!(
            get_remote_url(&repo, "origin").unwrap(),
            Some("https://github.com/test/repo2.git".to_string())
        );
    }

    #[test]
    fn test_pull_latest_missing_remote_errors() {
        let (_temp, repo) = setup_test_repo();

        let err = pull_latest(&repo, "origin").expect_err("pull should fail without remote");
        let message = err.to_string();
        assert!(
            message.contains("remote")
                || message.contains("NotFound")
                || message.contains("fatal")
                || message.contains("not found"),
            "unexpected error message: {}",
            message
        );
    }

    #[test]
    fn test_split_upstream_ref_valid() {
        let (remote, branch) = split_upstream_ref("origin/main").unwrap();
        assert_eq!(remote, "origin");
        assert_eq!(branch, "main");
    }

    #[test]
    fn test_split_upstream_ref_nested_branch() {
        let (remote, branch) = split_upstream_ref("origin/feat/my-feature").unwrap();
        assert_eq!(remote, "origin");
        assert_eq!(branch, "feat/my-feature");
    }

    #[test]
    fn test_split_upstream_ref_no_slash() {
        let result = split_upstream_ref("main");
        assert!(result.is_err());
    }

    #[test]
    fn test_split_upstream_ref_empty_parts() {
        assert!(split_upstream_ref("/main").is_err());
        assert!(split_upstream_ref("origin/").is_err());
    }

    #[test]
    fn test_interpret_push_error_non_fast_forward() {
        let msg = interpret_push_error(
            "error: failed to push some refs\n ! [rejected] main -> main (non-fast-forward)",
        );
        assert!(msg.contains("Pull first"));
        assert!(msg.contains("Original:"));
    }

    #[test]
    fn test_interpret_push_error_permission_denied() {
        let msg = interpret_push_error("Permission denied (publickey).");
        assert!(msg.contains("Authentication failed"));
        assert!(msg.contains("gh auth login"));
    }

    #[test]
    fn test_interpret_push_error_repo_not_found() {
        let msg = interpret_push_error("fatal: repository not found");
        assert!(msg.contains("Cannot reach remote"));
    }

    #[test]
    fn test_interpret_push_error_could_not_read() {
        let msg = interpret_push_error("fatal: Could not read from remote repository.");
        assert!(msg.contains("Cannot reach remote"));
    }

    #[test]
    fn test_interpret_push_error_unknown() {
        let msg = interpret_push_error("some other error");
        assert_eq!(msg, "some other error");
    }
}

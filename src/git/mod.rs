//! Git operations wrapper
//!
//! Provides a unified interface for git operations.
//! Uses git2 (libgit2 bindings) by default.
//! Can optionally use gitoxide (gix) with the "gitoxide" feature flag.

pub mod backend;
pub mod branch;
pub mod cache;
pub mod cherry_pick;
pub mod gc;
pub mod git2_backend;
pub mod remote;
pub mod status;

pub use branch::*;
pub use cache::{invalidate_status_cache, GitStatusCache, STATUS_CACHE};
pub use remote::*;
pub use status::*;

use crate::util::log_cmd;
use git2::Repository;
use std::path::Path;
use std::process::Command;
use thiserror::Error;

/// Errors that can occur during git operations
#[derive(Error, Debug)]
pub enum GitError {
    #[error("Git error: {0}")]
    Git(#[from] git2::Error),

    #[error("Repository not found: {0}")]
    NotFound(String),

    #[error("Not a git repository: {0}")]
    NotARepo(String),

    #[error("Branch not found: {0}")]
    BranchNotFound(String),

    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Operation failed: {0}")]
    OperationFailed(String),

    #[error("Reference error: {0}")]
    Reference(String),

    #[error("Object error: {0}")]
    Object(String),

    #[error("Repository locked: {0}")]
    RepositoryLocked(String),
}

/// Open a git repository at the given path
pub fn open_repo<P: AsRef<Path>>(path: P) -> Result<Repository, GitError> {
    Repository::open(path.as_ref())
        .map_err(|e| GitError::NotARepo(format!("{}: {}", path.as_ref().display(), e)))
}

/// Check if a path is a git repository
pub fn is_git_repo<P: AsRef<Path>>(path: P) -> bool {
    Repository::open(path.as_ref()).is_ok()
}

/// Check if a path exists
pub fn path_exists<P: AsRef<Path>>(path: P) -> bool {
    path.as_ref().exists()
}

/// Check if a git lock file exists (`.git/index.lock`).
///
/// Returns the lock file path if it exists.
pub fn git_lock_exists<P: AsRef<Path>>(path: P) -> Option<std::path::PathBuf> {
    let lock_path = path.as_ref().join(".git").join("index.lock");
    if lock_path.exists() {
        Some(lock_path)
    } else {
        None
    }
}

/// Wait for a git lock to be released with exponential backoff.
///
/// Returns `Ok(())` if the lock is released within the retry window,
/// or `Err(GitError::RepositoryLocked)` if it times out.
pub fn wait_for_git_lock<P: AsRef<Path>>(path: P) -> Result<(), GitError> {
    let path = path.as_ref();
    let max_attempts: u32 = 5;
    let initial_delay_ms: u64 = 200;
    let max_delay_ms: u64 = 5000;

    for attempt in 0..max_attempts {
        if git_lock_exists(path).is_none() {
            return Ok(());
        }

        let delay_ms = (initial_delay_ms * 2u64.pow(attempt)).min(max_delay_ms);
        std::thread::sleep(std::time::Duration::from_millis(delay_ms));
    }

    // Final check
    if git_lock_exists(path).is_none() {
        return Ok(());
    }

    Err(GitError::RepositoryLocked(format!(
        "{}: .git/index.lock exists — another git process may be running",
        path.display()
    )))
}

/// Clone a repository
///
/// If a branch is specified but doesn't exist on the remote, falls back to
/// cloning without a branch (using the remote's default branch).
pub fn clone_repo<P: AsRef<Path>>(
    url: &str,
    path: P,
    branch: Option<&str>,
) -> Result<Repository, GitError> {
    let path = path.as_ref();
    let path_str = path.to_str().unwrap_or(".");

    // Try cloning with specified branch first
    if let Some(b) = branch {
        let args = vec!["clone", "-b", b, url, path_str];

        let mut cmd = Command::new("git");
        cmd.args(&args);
        log_cmd(&cmd);
        let output = cmd
            .output()
            .map_err(|e| GitError::OperationFailed(e.to_string()))?;

        if output.status.success() {
            return open_repo(path);
        }

        // Check if failure was due to branch not found
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("Remote branch") && stderr.contains("not found") {
            // Fall through to clone without branch
        } else {
            // Other error - return it
            return Err(GitError::OperationFailed(format!(
                "git clone failed: {}",
                stderr
            )));
        }
    }

    // Clone without -b flag (uses remote's default branch)
    let args = vec!["clone", url, path_str];

    let mut cmd = Command::new("git");
    cmd.args(&args);
    log_cmd(&cmd);
    let output = cmd
        .output()
        .map_err(|e| GitError::OperationFailed(e.to_string()))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(GitError::OperationFailed(format!(
            "git clone failed: {}",
            stderr
        )));
    }

    open_repo(path)
}

/// Get the working directory of a repository
/// Works correctly for both regular repos and worktrees
pub fn get_workdir(repo: &Repository) -> &Path {
    repo.workdir().unwrap_or_else(|| repo.path())
}

/// Get the current branch name
pub fn get_current_branch(repo: &Repository) -> Result<String, GitError> {
    let head = repo
        .head()
        .map_err(|e| GitError::Reference(e.to_string()))?;

    if head.is_branch() {
        let name = head.shorthand().unwrap_or("HEAD");
        Ok(name.to_string())
    } else {
        // Detached HEAD
        let oid = head
            .target()
            .ok_or_else(|| GitError::Reference("HEAD has no target".to_string()))?;
        Ok(format!("(HEAD detached at {})", &oid.to_string()[..7]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    #[test]
    fn test_is_git_repo() {
        let temp = TempDir::new().unwrap();
        assert!(!is_git_repo(temp.path()));

        // Initialize a git repo
        Repository::init(temp.path()).unwrap();
        assert!(is_git_repo(temp.path()));
    }

    #[test]
    fn test_path_exists() {
        let temp = TempDir::new().unwrap();
        assert!(path_exists(temp.path()));
        assert!(!path_exists(temp.path().join("nonexistent")));
    }

    #[test]
    fn test_open_repo() {
        let temp = TempDir::new().unwrap();

        // Should fail for non-repo
        assert!(open_repo(temp.path()).is_err());

        // Should succeed after init
        Repository::init(temp.path()).unwrap();
        assert!(open_repo(temp.path()).is_ok());
    }

    fn git(dir: &std::path::Path, args: &[&str]) {
        let output = Command::new("git")
            .current_dir(dir)
            .args(args)
            .output()
            .unwrap_or_else(|e| panic!("failed to run git {:?}: {}", args, e));
        assert!(
            output.status.success(),
            "git {:?} failed in {}: {}",
            args,
            dir.display(),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn setup_bare_remote() -> (TempDir, String) {
        let temp = TempDir::new().unwrap();
        let bare_path = temp.path().join("remote.git");

        let output = Command::new("git")
            .args(["init", "--bare", "-b", "main", bare_path.to_str().unwrap()])
            .current_dir(temp.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git init --bare failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let staging = temp.path().join("staging");
        fs::create_dir_all(&staging).unwrap();
        git(&staging, &["init", "-b", "main"]);
        git(&staging, &["config", "user.email", "test@example.com"]);
        git(&staging, &["config", "user.name", "Test User"]);
        fs::write(staging.join("README.md"), "# Test").unwrap();
        git(&staging, &["add", "README.md"]);
        git(&staging, &["commit", "-m", "Initial commit"]);
        git(
            &staging,
            &[
                "remote",
                "add",
                "origin",
                &format!("file://{}", bare_path.display()),
            ],
        );
        git(&staging, &["push", "-u", "origin", "main"]);

        (temp, format!("file://{}", bare_path.display()))
    }

    #[test]
    fn test_clone_repo_invalid_url_fails() {
        let temp = TempDir::new().unwrap();
        let dest = temp.path().join("dest");

        let result = clone_repo("file:///does-not-exist/repo.git", &dest, Some("main"));
        assert!(result.is_err(), "expected clone to fail for bad URL");
    }

    #[test]
    fn test_clone_repo_falls_back_when_branch_missing() {
        let (_temp, remote_url) = setup_bare_remote();
        let dest_root = TempDir::new().unwrap();
        let dest = dest_root.path().join("dest");

        let result = clone_repo(&remote_url, &dest, Some("does-not-exist"));
        assert!(
            result.is_ok(),
            "expected clone to fall back to default branch"
        );

        let repo = open_repo(&dest).expect("open repo");
        let branch = get_current_branch(&repo).expect("current branch");
        assert_eq!(branch, "main");
    }

    #[test]
    fn test_git_lock_exists_none() {
        let temp = TempDir::new().unwrap();
        Repository::init(temp.path()).unwrap();
        assert!(git_lock_exists(temp.path()).is_none());
    }

    #[test]
    fn test_git_lock_exists_some() {
        let temp = TempDir::new().unwrap();
        Repository::init(temp.path()).unwrap();
        let lock_path = temp.path().join(".git").join("index.lock");
        fs::write(&lock_path, "").unwrap();
        assert!(git_lock_exists(temp.path()).is_some());
    }

    #[test]
    fn test_wait_for_git_lock_no_lock() {
        let temp = TempDir::new().unwrap();
        Repository::init(temp.path()).unwrap();
        assert!(wait_for_git_lock(temp.path()).is_ok());
    }

    #[test]
    fn test_wait_for_git_lock_persistent_lock() {
        let temp = TempDir::new().unwrap();
        Repository::init(temp.path()).unwrap();
        let lock_path = temp.path().join(".git").join("index.lock");
        fs::write(&lock_path, "").unwrap();

        let result = wait_for_git_lock(temp.path());
        assert!(result.is_err());
        let err_msg = result.unwrap_err().to_string();
        assert!(err_msg.contains("index.lock"));
    }
}

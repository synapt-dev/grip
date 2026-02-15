//! Git backend abstraction
//!
//! Defines [`GitBackend`] and [`GitRepo`] traits so the git layer can be
//! swapped (git2 → gitoxide) or mocked in tests without touching the
//! filesystem.
//!
//! The default implementation ([`Git2Backend`](super::git2_backend::Git2Backend))
//! delegates to the existing free functions in `src/git/`.

use std::path::Path;

use super::remote::SafePullResult;
use super::status::RepoStatusInfo;
use super::GitError;

/// Factory for opening / cloning repositories.
pub trait GitBackend: Send + Sync {
    /// Open an existing repository at `path`.
    fn open_repo(&self, path: &Path) -> Result<Box<dyn GitRepo>, GitError>;

    /// Clone a remote repository into `path`.
    fn clone_repo(
        &self,
        url: &str,
        path: &Path,
        branch: Option<&str>,
    ) -> Result<Box<dyn GitRepo>, GitError>;

    /// Quick check: is `path` the root of a git repository?
    fn is_git_repo(&self, path: &Path) -> bool;
}

/// Operations on an already-opened repository.
pub trait GitRepo: Send {
    // ── identity ───────────────────────────────────────────────────────

    /// Filesystem path of the working directory.
    fn workdir(&self) -> &Path;

    /// Current branch name (or detached HEAD description).
    fn current_branch(&self) -> Result<String, GitError>;

    /// SHA of the HEAD commit.
    fn head_commit_id(&self) -> Result<String, GitError>;

    // ── branch operations ──────────────────────────────────────────────

    /// Create a new branch and check it out.
    fn create_and_checkout_branch(&self, name: &str) -> Result<(), GitError>;

    /// Check out an existing branch.
    fn checkout_branch(&self, name: &str) -> Result<(), GitError>;

    /// Does a local branch with this name exist?
    fn branch_exists(&self, name: &str) -> bool;

    /// Does a remote-tracking branch exist?
    fn remote_branch_exists(&self, name: &str, remote: &str) -> bool;

    /// Delete a local branch.
    fn delete_branch(&self, name: &str, force: bool) -> Result<(), GitError>;

    /// List local branch names.
    fn list_local_branches(&self) -> Result<Vec<String>, GitError>;

    // ── remote operations ──────────────────────────────────────────────

    /// Fetch from a remote.
    fn fetch(&self, remote: &str) -> Result<(), GitError>;

    /// Pull (merge mode).
    fn pull(&self, remote: &str) -> Result<(), GitError>;

    /// Push a branch to a remote.
    fn push(&self, branch: &str, remote: &str, set_upstream: bool) -> Result<(), GitError>;

    /// Get the URL configured for a remote.
    fn get_remote_url(&self, remote: &str) -> Result<Option<String>, GitError>;

    // ── working-tree queries ───────────────────────────────────────────

    /// Porcelain status (staged, modified, untracked, ahead/behind).
    fn status(&self) -> Result<RepoStatusInfo, GitError>;

    /// Hard-reset to a ref.
    fn reset_hard(&self, target: &str) -> Result<(), GitError>;

    /// Safe pull that handles deleted upstream branches.
    fn safe_pull(&self, default_branch: &str, remote: &str) -> Result<SafePullResult, GitError>;
}

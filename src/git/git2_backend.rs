//! Git2 (libgit2) implementation of [`GitBackend`] / [`GitRepo`].
//!
//! Every method delegates to the existing free functions in `src/git/`,
//! so behaviour is identical to calling them directly. This wrapper exists
//! so new code can program against the trait, and tests can substitute a
//! mock backend.

use std::path::{Path, PathBuf};

use git2::Repository;

use super::backend::{GitBackend, GitRepo};
use super::remote::SafePullResult;
use super::status::RepoStatusInfo;
use super::GitError;

// ── Backend (factory) ──────────────────────────────────────────────────

/// Default backend backed by libgit2 + `git` CLI.
pub struct Git2Backend;

impl GitBackend for Git2Backend {
    fn open_repo(&self, path: &Path) -> Result<Box<dyn GitRepo>, GitError> {
        let repo = super::open_repo(path)?;
        Ok(Box::new(Git2Repo::new(repo)))
    }

    fn clone_repo(
        &self,
        url: &str,
        path: &Path,
        branch: Option<&str>,
    ) -> Result<Box<dyn GitRepo>, GitError> {
        let repo = super::clone_repo(url, path, branch)?;
        Ok(Box::new(Git2Repo::new(repo)))
    }

    fn is_git_repo(&self, path: &Path) -> bool {
        super::is_git_repo(path)
    }
}

// ── Repo handle ────────────────────────────────────────────────────────

/// Wraps a `git2::Repository` and delegates every operation to the
/// existing free functions in `crate::git`.
struct Git2Repo {
    repo: Repository,
    /// Cached working-directory path (avoids repeated lookups).
    workdir: PathBuf,
}

impl Git2Repo {
    fn new(repo: Repository) -> Self {
        let workdir = super::get_workdir(&repo).to_path_buf();
        Self { repo, workdir }
    }
}

impl GitRepo for Git2Repo {
    // ── identity ───────────────────────────────────────────────────────

    fn workdir(&self) -> &Path {
        &self.workdir
    }

    fn current_branch(&self) -> Result<String, GitError> {
        super::get_current_branch(&self.repo)
    }

    fn head_commit_id(&self) -> Result<String, GitError> {
        let head = self
            .repo
            .head()
            .map_err(|e| GitError::Reference(e.to_string()))?;
        let oid = head
            .target()
            .ok_or_else(|| GitError::Reference("HEAD has no target".to_string()))?;
        Ok(oid.to_string())
    }

    // ── branch operations ──────────────────────────────────────────────

    fn create_and_checkout_branch(&self, name: &str) -> Result<(), GitError> {
        super::branch::create_and_checkout_branch(&self.repo, name)
    }

    fn checkout_branch(&self, name: &str) -> Result<(), GitError> {
        super::branch::checkout_branch(&self.repo, name)
    }

    fn branch_exists(&self, name: &str) -> bool {
        super::branch::branch_exists(&self.repo, name)
    }

    fn remote_branch_exists(&self, name: &str, remote: &str) -> bool {
        super::branch::remote_branch_exists(&self.repo, name, remote)
    }

    fn delete_branch(&self, name: &str, force: bool) -> Result<(), GitError> {
        super::branch::delete_local_branch(&self.repo, name, force)
    }

    fn list_local_branches(&self) -> Result<Vec<String>, GitError> {
        super::branch::list_local_branches(&self.repo)
    }

    // ── remote operations ──────────────────────────────────────────────

    fn fetch(&self, remote: &str) -> Result<(), GitError> {
        super::remote::fetch_remote(&self.repo, remote)
    }

    fn pull(&self, remote: &str) -> Result<(), GitError> {
        super::remote::pull_latest(&self.repo, remote)
    }

    fn push(&self, branch: &str, remote: &str, set_upstream: bool) -> Result<(), GitError> {
        super::remote::push_branch(&self.repo, branch, remote, set_upstream)
    }

    fn get_remote_url(&self, remote: &str) -> Result<Option<String>, GitError> {
        super::remote::get_remote_url(&self.repo, remote)
    }

    // ── working-tree queries ───────────────────────────────────────────

    fn status(&self) -> Result<RepoStatusInfo, GitError> {
        super::status::get_status_info(&self.repo)
    }

    fn reset_hard(&self, target: &str) -> Result<(), GitError> {
        super::remote::reset_hard(&self.repo, target)
    }

    fn safe_pull(&self, default_branch: &str, remote: &str) -> Result<SafePullResult, GitError> {
        super::remote::safe_pull_latest(&self.repo, default_branch, remote)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    fn setup_git_repo() -> TempDir {
        let temp = TempDir::new().unwrap();
        let dir = temp.path();

        Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(dir)
            .output()
            .unwrap();
        Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(dir)
            .output()
            .unwrap();
        Command::new("git")
            .args(["config", "user.name", "Test User"])
            .current_dir(dir)
            .output()
            .unwrap();
        fs::write(dir.join("README.md"), "# Test").unwrap();
        Command::new("git")
            .args(["add", "README.md"])
            .current_dir(dir)
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Initial commit"])
            .current_dir(dir)
            .output()
            .unwrap();

        temp
    }

    #[test]
    fn test_backend_open_and_read_head() {
        let temp = setup_git_repo();
        let backend = Git2Backend;

        let repo = backend.open_repo(temp.path()).unwrap();
        assert_eq!(repo.current_branch().unwrap(), "main");
        assert!(!repo.head_commit_id().unwrap().is_empty());
        // Compare canonical paths to handle macOS /var -> /private/var symlink
        assert_eq!(
            repo.workdir().canonicalize().unwrap(),
            temp.path().canonicalize().unwrap()
        );
    }

    #[test]
    fn test_backend_is_git_repo() {
        let temp = setup_git_repo();
        let backend = Git2Backend;

        assert!(backend.is_git_repo(temp.path()));

        let non_repo = TempDir::new().unwrap();
        assert!(!backend.is_git_repo(non_repo.path()));
    }

    #[test]
    fn test_backend_branch_operations() {
        let temp = setup_git_repo();
        let backend = Git2Backend;
        let repo = backend.open_repo(temp.path()).unwrap();

        assert!(!repo.branch_exists("feature"));

        repo.create_and_checkout_branch("feature").unwrap();
        assert!(repo.branch_exists("feature"));
        assert_eq!(repo.current_branch().unwrap(), "feature");

        let branches = repo.list_local_branches().unwrap();
        assert!(branches.contains(&"main".to_string()));
        assert!(branches.contains(&"feature".to_string()));

        repo.checkout_branch("main").unwrap();
        assert_eq!(repo.current_branch().unwrap(), "main");

        repo.delete_branch("feature", false).unwrap();
        assert!(!repo.branch_exists("feature"));
    }

    #[test]
    fn test_backend_status() {
        let temp = setup_git_repo();
        let backend = Git2Backend;
        let repo = backend.open_repo(temp.path()).unwrap();

        let status = repo.status().unwrap();
        assert!(status.is_clean);

        // Create an untracked file
        fs::write(temp.path().join("new.txt"), "hello").unwrap();
        let status = repo.status().unwrap();
        assert!(!status.is_clean);
        assert_eq!(status.untracked.len(), 1);
    }

    #[test]
    fn test_backend_open_nonexistent_fails() {
        let backend = Git2Backend;
        assert!(backend.open_repo(Path::new("/nonexistent")).is_err());
    }
}

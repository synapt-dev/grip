//! Workspace cache — bare-repo cache layer for manifest repos
//!
//! Each manifest repo gets a bare clone under `.grip/cache/<name>.git`.
//! These caches serve as fast local references for creating agent workspaces
//! and manual checkouts without sharing mutable .git state.

use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::util::log_cmd;

/// Directory name under .grip/ where bare caches live.
const CACHE_DIR: &str = "cache";

/// Resolve the cache path for a repo: `<workspace_root>/.grip/cache/<name>.git`
pub fn cache_path(workspace_root: &Path, repo_name: &str) -> PathBuf {
    workspace_root
        .join(".grip")
        .join(CACHE_DIR)
        .join(format!("{}.git", repo_name))
}

/// Check whether a bare cache exists for the given repo.
pub fn cache_exists(workspace_root: &Path, repo_name: &str) -> bool {
    let path = cache_path(workspace_root, repo_name);
    // A valid bare repo has a HEAD file
    path.join("HEAD").is_file()
}

/// Bootstrap a bare cache by cloning from the canonical remote.
///
/// Creates `.grip/cache/<name>.git` as a bare clone of `url`.
/// If the cache already exists, this is a no-op (use `update_cache` to fetch).
pub fn bootstrap_cache(workspace_root: &Path, repo_name: &str, url: &str) -> Result<()> {
    let path = cache_path(workspace_root, repo_name);

    if cache_exists(workspace_root, repo_name) {
        return Ok(());
    }

    // Ensure parent directory exists
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating cache directory: {}", parent.display()))?;
    }

    let mut cmd = Command::new("git");
    cmd.args(["clone", "--bare", url]).arg(&path);
    log_cmd(&cmd);

    let output = cmd
        .output()
        .with_context(|| format!("running git clone --bare for {}", repo_name))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!(
            "failed to bootstrap cache for {}: {}",
            repo_name,
            stderr.trim()
        );
    }

    Ok(())
}

/// Fetch latest refs into an existing bare cache.
///
/// Runs `git fetch --all --prune` inside the bare repo to bring it up to date.
pub fn update_cache(workspace_root: &Path, repo_name: &str) -> Result<()> {
    let path = cache_path(workspace_root, repo_name);

    if !cache_exists(workspace_root, repo_name) {
        anyhow::bail!("cache does not exist for {}: {}", repo_name, path.display());
    }

    let mut cmd = Command::new("git");
    cmd.args(["fetch", "--all", "--prune"]).current_dir(&path);
    log_cmd(&cmd);

    let output = cmd
        .output()
        .with_context(|| format!("fetching cache for {}", repo_name))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!(
            "failed to update cache for {}: {}",
            repo_name,
            stderr.trim()
        );
    }

    Ok(())
}

/// Get the remote URL stored in a bare cache.
pub fn cache_remote_url(workspace_root: &Path, repo_name: &str) -> Result<Option<String>> {
    let path = cache_path(workspace_root, repo_name);

    if !cache_exists(workspace_root, repo_name) {
        return Ok(None);
    }

    let mut cmd = Command::new("git");
    cmd.args(["remote", "get-url", "origin"]).current_dir(&path);
    log_cmd(&cmd);

    let output = cmd
        .output()
        .with_context(|| format!("reading cache remote for {}", repo_name))?;

    if output.status.success() {
        Ok(Some(
            String::from_utf8_lossy(&output.stdout).trim().to_string(),
        ))
    } else {
        Ok(None)
    }
}

/// Bootstrap caches for all repos in a manifest.
///
/// Takes an iterator of (name, url) pairs. Skips repos that already have caches.
/// Returns the count of newly bootstrapped caches.
pub fn bootstrap_all<'a>(
    workspace_root: &Path,
    repos: impl Iterator<Item = (&'a str, &'a str)>,
) -> Result<usize> {
    let mut count = 0;
    for (name, url) in repos {
        if !cache_exists(workspace_root, name) {
            bootstrap_cache(workspace_root, name, url)?;
            count += 1;
        }
    }
    Ok(count)
}

/// Update all existing caches under `.grip/cache/`.
///
/// Returns the count of caches updated.
pub fn update_all(workspace_root: &Path) -> Result<usize> {
    let cache_dir = workspace_root.join(".grip").join(CACHE_DIR);
    if !cache_dir.is_dir() {
        return Ok(0);
    }

    let mut count = 0;
    for entry in std::fs::read_dir(&cache_dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        // Cache dirs are named <repo>.git
        if name_str.ends_with(".git") && entry.path().join("HEAD").is_file() {
            let repo_name = name_str.trim_end_matches(".git");
            update_cache(workspace_root, repo_name)?;
            count += 1;
        }
    }
    Ok(count)
}

/// Remove a single repo cache.
pub fn remove_cache(workspace_root: &Path, repo_name: &str) -> Result<bool> {
    let path = cache_path(workspace_root, repo_name);
    if path.is_dir() {
        std::fs::remove_dir_all(&path)
            .with_context(|| format!("removing cache: {}", path.display()))?;
        Ok(true)
    } else {
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// Helper: create a temporary "remote" repo to clone from
    fn create_test_remote(dir: &Path) -> PathBuf {
        let remote_path = dir.join("remote-repo.git");
        // Init a bare repo to act as the remote
        Command::new("git")
            .args(["init", "--bare"])
            .arg(&remote_path)
            .output()
            .expect("git init --bare");

        // Create a non-bare repo, add a commit, push to the bare repo
        let work_path = dir.join("work-repo");
        Command::new("git")
            .args(["init"])
            .arg(&work_path)
            .output()
            .expect("git init");
        Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&work_path)
            .output()
            .expect("git config email");
        Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&work_path)
            .output()
            .expect("git config name");
        fs::write(work_path.join("README.md"), "# test").expect("write file");
        Command::new("git")
            .args(["add", "."])
            .current_dir(&work_path)
            .output()
            .expect("git add");
        Command::new("git")
            .args(["commit", "-m", "initial"])
            .current_dir(&work_path)
            .output()
            .expect("git commit");
        Command::new("git")
            .args(["remote", "add", "origin"])
            .arg(&remote_path)
            .current_dir(&work_path)
            .output()
            .expect("git remote add");
        Command::new("git")
            .args(["push", "origin", "main"])
            .current_dir(&work_path)
            .output()
            .ok(); // might be master, not main
        Command::new("git")
            .args(["push", "origin", "master"])
            .current_dir(&work_path)
            .output()
            .ok();

        remote_path
    }

    #[test]
    fn test_cache_path() {
        let root = Path::new("/workspace");
        let path = cache_path(root, "myrepo");
        assert_eq!(path, PathBuf::from("/workspace/.grip/cache/myrepo.git"));
    }

    #[test]
    fn test_cache_does_not_exist_initially() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(!cache_exists(tmp.path(), "nonexistent"));
    }

    #[test]
    fn test_bootstrap_and_exists() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        assert!(!cache_exists(&workspace, "testrepo"));

        bootstrap_cache(&workspace, "testrepo", &url).expect("bootstrap");
        assert!(cache_exists(&workspace, "testrepo"));

        // Verify it's a bare repo
        let cp = cache_path(&workspace, "testrepo");
        assert!(cp.join("HEAD").is_file());
        assert!(!cp.join(".git").exists()); // bare repos don't have .git subdir
    }

    #[test]
    fn test_bootstrap_is_idempotent() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        bootstrap_cache(&workspace, "repo", &url).expect("bootstrap 1");
        bootstrap_cache(&workspace, "repo", &url).expect("bootstrap 2"); // no-op
        assert!(cache_exists(&workspace, "repo"));
    }

    #[test]
    fn test_update_cache() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        bootstrap_cache(&workspace, "repo", &url).expect("bootstrap");
        update_cache(&workspace, "repo").expect("update");
    }

    #[test]
    fn test_update_nonexistent_fails() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let result = update_cache(tmp.path(), "nope");
        assert!(result.is_err());
    }

    #[test]
    fn test_cache_remote_url() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        bootstrap_cache(&workspace, "repo", &url).expect("bootstrap");

        let stored_url = cache_remote_url(&workspace, "repo")
            .expect("get url")
            .expect("has url");
        assert_eq!(stored_url, url);
    }

    #[test]
    fn test_remove_cache() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        bootstrap_cache(&workspace, "repo", &url).expect("bootstrap");
        assert!(cache_exists(&workspace, "repo"));

        let removed = remove_cache(&workspace, "repo").expect("remove");
        assert!(removed);
        assert!(!cache_exists(&workspace, "repo"));
    }

    #[test]
    fn test_remove_nonexistent_returns_false() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let removed = remove_cache(tmp.path(), "nope").expect("remove");
        assert!(!removed);
    }

    #[test]
    fn test_bootstrap_all() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        let repos = vec![("repo1", url.as_str()), ("repo2", url.as_str())];
        let count = bootstrap_all(&workspace, repos.into_iter()).expect("bootstrap all");
        assert_eq!(count, 2);
        assert!(cache_exists(&workspace, "repo1"));
        assert!(cache_exists(&workspace, "repo2"));

        // Second call: no new bootstraps
        let repos2 = vec![("repo1", url.as_str()), ("repo2", url.as_str())];
        let count2 = bootstrap_all(&workspace, repos2.into_iter()).expect("bootstrap all 2");
        assert_eq!(count2, 0);
    }
}

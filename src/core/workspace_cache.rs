//! Workspace cache — bare-repo cache layer for manifest repos
//!
//! Caches now live at a machine-level root by default (`~/.grip/cache/`),
//! keyed by normalized remote URL rather than workspace-local repo name.
//! This lets multiple workspaces reuse the same object store without sharing
//! mutable `.git` state between checkouts.

use anyhow::{Context, Result};
use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::util::log_cmd;

const CACHE_ENV_VAR: &str = "GRIP_CACHE_DIR";
const GRIP_DIR: &str = ".grip";
const CACHE_DIR: &str = "cache";

fn home_dir() -> Result<PathBuf> {
    if let Some(home) = env::var_os("HOME") {
        return Ok(PathBuf::from(home));
    }
    if let Some(profile) = env::var_os("USERPROFILE") {
        return Ok(PathBuf::from(profile));
    }
    anyhow::bail!("could not resolve home directory for global cache root")
}

/// Resolve the machine-level cache root.
pub fn cache_root() -> Result<PathBuf> {
    if let Some(override_dir) = env::var_os(CACHE_ENV_VAR) {
        return Ok(PathBuf::from(override_dir));
    }
    Ok(home_dir()?.join(GRIP_DIR).join(CACHE_DIR))
}

fn legacy_cache_path(workspace_root: &Path, repo_name: &str) -> PathBuf {
    workspace_root
        .join(GRIP_DIR)
        .join(CACHE_DIR)
        .join(format!("{}.git", repo_name))
}

fn normalize_git_url(url: &str) -> String {
    let trimmed = url.trim().trim_end_matches('/').trim_end_matches(".git");

    if !trimmed.contains("://") {
        if let Some((user_host, path)) = trimmed.split_once(':') {
            let host = user_host.rsplit('@').next().unwrap_or(user_host);
            if !host.is_empty() && !path.is_empty() {
                return format!(
                    "{}:{}",
                    host.to_ascii_lowercase(),
                    path.trim_start_matches('/')
                );
            }
        }
    }

    if let Some((_, rest)) = trimmed.split_once("://") {
        if let Some((host_user, path)) = rest.split_once('/') {
            let host = host_user.rsplit('@').next().unwrap_or(host_user);
            if !host.is_empty() && !path.is_empty() {
                return format!(
                    "{}:{}",
                    host.to_ascii_lowercase(),
                    path.trim_start_matches('/')
                );
            }
        }
    }

    trimmed.to_string()
}

/// Stable filesystem-safe cache key derived from a normalized remote URL.
pub fn cache_key(url: &str) -> String {
    let normalized = normalize_git_url(url);
    let mut key = String::with_capacity(normalized.len());
    let mut last_was_sep = false;

    for ch in normalized.chars() {
        if ch.is_ascii_alphanumeric() {
            key.push(ch.to_ascii_lowercase());
            last_was_sep = false;
        } else if !last_was_sep {
            key.push('_');
            last_was_sep = true;
        }
    }

    key.trim_matches('_').to_string()
}

/// Resolve the primary global cache path for a repo URL.
pub fn cache_path(url: &str) -> Result<PathBuf> {
    Ok(cache_root()?.join(format!("{}.git", cache_key(url))))
}

fn cache_is_valid(path: &Path) -> bool {
    path.join("HEAD").is_file()
}

/// Resolve the cache path to use, preferring the global cache but falling back
/// to an existing legacy workspace-local cache.
pub fn resolve_cache_path(workspace_root: &Path, repo_name: &str, url: &str) -> Result<PathBuf> {
    let global = cache_path(url)?;
    if cache_is_valid(&global) {
        return Ok(global);
    }

    let legacy = legacy_cache_path(workspace_root, repo_name);
    if cache_is_valid(&legacy) {
        return Ok(legacy);
    }

    Ok(global)
}

/// Check whether a cache exists for the given repo.
pub fn cache_exists(workspace_root: &Path, repo_name: &str, url: &str) -> Result<bool> {
    Ok(cache_is_valid(&resolve_cache_path(
        workspace_root,
        repo_name,
        url,
    )?))
}

/// Bootstrap a bare cache by cloning from the canonical remote.
pub fn bootstrap_cache(workspace_root: &Path, repo_name: &str, url: &str) -> Result<()> {
    let existing = resolve_cache_path(workspace_root, repo_name, url)?;
    if cache_is_valid(&existing) {
        return Ok(());
    }

    let path = cache_path(url)?;
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
pub fn update_cache(workspace_root: &Path, repo_name: &str, url: &str) -> Result<()> {
    let path = resolve_cache_path(workspace_root, repo_name, url)?;

    if !cache_is_valid(&path) {
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
pub fn cache_remote_url(
    workspace_root: &Path,
    repo_name: &str,
    url: &str,
) -> Result<Option<String>> {
    let path = resolve_cache_path(workspace_root, repo_name, url)?;

    if !cache_is_valid(&path) {
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
pub fn bootstrap_all<'a>(
    workspace_root: &Path,
    repos: impl Iterator<Item = (&'a str, &'a str)>,
) -> Result<usize> {
    let mut count = 0;
    for (name, url) in repos {
        if !cache_exists(workspace_root, name, url)? {
            bootstrap_cache(workspace_root, name, url)?;
            count += 1;
        }
    }
    Ok(count)
}

/// Update all caches for repos in the current manifest.
pub fn update_all<'a>(
    workspace_root: &Path,
    repos: impl Iterator<Item = (&'a str, &'a str)>,
) -> Result<usize> {
    let mut count = 0;
    for (name, url) in repos {
        if cache_exists(workspace_root, name, url)? {
            update_cache(workspace_root, name, url)?;
            count += 1;
        }
    }
    Ok(count)
}

/// Remove a single repo cache.
pub fn remove_cache(workspace_root: &Path, repo_name: &str, url: &str) -> Result<bool> {
    let path = resolve_cache_path(workspace_root, repo_name, url)?;
    if path.is_dir() {
        std::fs::remove_dir_all(&path)
            .with_context(|| format!("removing cache: {}", path.display()))?;
        Ok(true)
    } else {
        Ok(false)
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    use once_cell::sync::Lazy;
    use std::sync::Mutex;

    pub(crate) static ENV_LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn with_cache_dir<T>(cache_dir: &Path, f: impl FnOnce() -> T) -> T {
        let _guard = test_support::ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let previous = env::var_os(CACHE_ENV_VAR);
        env::set_var(CACHE_ENV_VAR, cache_dir);
        let result = f();
        match previous {
            Some(value) => env::set_var(CACHE_ENV_VAR, value),
            None => env::remove_var(CACHE_ENV_VAR),
        }
        result
    }

    fn create_test_remote(dir: &Path) -> PathBuf {
        let remote_path = dir.join("remote-repo.git");
        Command::new("git")
            .args(["init", "--bare"])
            .arg(&remote_path)
            .output()
            .expect("git init --bare");

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
            .ok();
        Command::new("git")
            .args(["push", "origin", "master"])
            .current_dir(&work_path)
            .output()
            .ok();

        remote_path
    }

    #[test]
    fn test_cache_key_normalizes_remote_url_forms() {
        let ssh = cache_key("git@github.com:OpenAI/myrepo.git");
        let https = cache_key("https://github.com/OpenAI/myrepo.git");
        assert_eq!(ssh, "github_com_openai_myrepo");
        assert_eq!(ssh, https);
    }

    #[test]
    fn test_cache_path_uses_global_root() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let path = cache_path("git@github.com:OpenAI/myrepo.git").expect("cache path");
            assert_eq!(path, cache_dir.join("github_com_openai_myrepo.git"));
        });
    }

    #[test]
    fn test_cache_does_not_exist_initially() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");
        with_cache_dir(&cache_dir, || {
            assert!(!cache_exists(
                &workspace,
                "nonexistent",
                "git@github.com:user/nonexistent.git"
            )
            .expect("cache exists"));
        });
    }

    #[test]
    fn test_bootstrap_and_exists() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        with_cache_dir(&cache_dir, || {
            assert!(!cache_exists(&workspace, "testrepo", &url).expect("cache exists before"));

            bootstrap_cache(&workspace, "testrepo", &url).expect("bootstrap");
            assert!(cache_exists(&workspace, "testrepo", &url).expect("cache exists after"));

            let cp = cache_path(&url).expect("cache path");
            assert!(cp.join("HEAD").is_file());
            assert!(!cp.join(".git").exists());
        });
    }

    #[test]
    fn test_bootstrap_is_idempotent() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        with_cache_dir(&cache_dir, || {
            bootstrap_cache(&workspace, "repo", &url).expect("bootstrap 1");
            bootstrap_cache(&workspace, "repo", &url).expect("bootstrap 2");
            assert!(cache_exists(&workspace, "repo", &url).expect("cache exists"));
        });
    }

    #[test]
    fn test_update_cache() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        with_cache_dir(&cache_dir, || {
            bootstrap_cache(&workspace, "repo", &url).expect("bootstrap");
            update_cache(&workspace, "repo", &url).expect("update");
        });
    }

    #[test]
    fn test_update_nonexistent_fails() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");
        with_cache_dir(&cache_dir, || {
            let result = update_cache(&workspace, "nope", "git@github.com:user/nope.git");
            assert!(result.is_err());
        });
    }

    #[test]
    fn test_cache_remote_url() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        with_cache_dir(&cache_dir, || {
            bootstrap_cache(&workspace, "repo", &url).expect("bootstrap");

            let stored_url = cache_remote_url(&workspace, "repo", &url)
                .expect("get url")
                .expect("has url");
            assert_eq!(stored_url, url);
        });
    }

    #[test]
    fn test_remove_cache() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        with_cache_dir(&cache_dir, || {
            bootstrap_cache(&workspace, "repo", &url).expect("bootstrap");
            assert!(cache_exists(&workspace, "repo", &url).expect("cache exists"));

            let removed = remove_cache(&workspace, "repo", &url).expect("remove");
            assert!(removed);
            assert!(!cache_exists(&workspace, "repo", &url).expect("cache removed"));
        });
    }

    #[test]
    fn test_remove_nonexistent_returns_false() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");
        with_cache_dir(&cache_dir, || {
            let removed =
                remove_cache(&workspace, "nope", "git@github.com:user/nope.git").expect("remove");
            assert!(!removed);
        });
    }

    #[test]
    fn test_bootstrap_all() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let remote = create_test_remote(tmp.path());
        let workspace = tmp.path().join("workspace");
        let cache_dir = tmp.path().join("global-cache");
        fs::create_dir_all(&workspace).expect("mkdir workspace");

        let url = remote.to_string_lossy().to_string();
        with_cache_dir(&cache_dir, || {
            let repos = vec![("repo1", url.as_str()), ("repo2", url.as_str())];
            let count = bootstrap_all(&workspace, repos.into_iter()).expect("bootstrap all");
            assert_eq!(count, 1);
            assert!(cache_exists(&workspace, "repo1", &url).expect("repo1 cached"));
            assert!(cache_exists(&workspace, "repo2", &url).expect("repo2 cached"));

            let repos2 = vec![("repo1", url.as_str()), ("repo2", url.as_str())];
            let count2 = bootstrap_all(&workspace, repos2.into_iter()).expect("bootstrap all 2");
            assert_eq!(count2, 0);
        });
    }

    #[test]
    fn test_resolve_cache_path_falls_back_to_legacy_workspace_cache() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let workspace = tmp.path().join("workspace");
        let legacy = workspace.join(".grip/cache/repo.git");
        fs::create_dir_all(&legacy).expect("mkdir legacy cache");
        fs::write(legacy.join("HEAD"), "ref: refs/heads/main\n").expect("write head");

        let resolved = resolve_cache_path(&workspace, "repo", "git@github.com:org/repo.git")
            .expect("resolve path");
        assert_eq!(resolved, legacy);
    }
}

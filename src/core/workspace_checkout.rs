//! Workspace checkouts — independent child clones materialized from the cache
//!
//! Each checkout lives under `.grip/checkouts/<name>/` and contains full clones
//! of manifest repos, created with `--reference` to reuse objects from the
//! bare cache. Checkouts are independently disposable.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::core::workspace_cache;
use crate::util::log_cmd;

/// Directory name under .grip/ where checkouts live.
const CHECKOUTS_DIR: &str = "checkouts";

/// Metadata for a single checkout.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckoutInfo {
    pub name: String,
    pub path: PathBuf,
    pub repos: Vec<CheckoutRepo>,
    pub created_at: String,
}

/// A single repo within a checkout.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckoutRepo {
    pub name: String,
    pub path: PathBuf,
    pub branch: Option<String>,
}

/// Resolve the checkout root: `<workspace_root>/.grip/checkouts/<name>/`
pub fn checkout_path(workspace_root: &Path, name: &str) -> PathBuf {
    workspace_root.join(".grip").join(CHECKOUTS_DIR).join(name)
}

/// Check whether a checkout exists.
pub fn checkout_exists(workspace_root: &Path, name: &str) -> bool {
    checkout_path(workspace_root, name).is_dir()
}

/// Materialize a single repo into a checkout from the cache.
///
/// Uses `git clone --reference <cache> <url> <target>` if a cache exists,
/// otherwise falls back to a direct clone.
/// Optionally checks out a specific branch.
pub fn materialize_repo(
    workspace_root: &Path,
    checkout_name: &str,
    repo_name: &str,
    repo_url: &str,
    repo_path: &str,
    branch: Option<&str>,
) -> Result<PathBuf> {
    let checkout_root = checkout_path(workspace_root, checkout_name);
    let target = checkout_root.join(repo_path);

    if target.join(".git").exists() {
        // Already materialized
        return Ok(target);
    }

    // Ensure parent directory exists
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating checkout dir: {}", parent.display()))?;
    }

    let cache = workspace_cache::resolve_cache_path(workspace_root, repo_name, repo_url)?;
    let has_cache = workspace_cache::cache_exists(workspace_root, repo_name, repo_url)?;

    let mut cmd = Command::new("git");
    cmd.arg("clone");

    // Use cache as reference if available (fast, saves disk via hardlinks)
    if has_cache {
        cmd.args(["--reference", &cache.to_string_lossy()]);
    }

    // Optionally specify branch
    if let Some(b) = branch {
        cmd.args(["--branch", b]);
    }

    cmd.arg(repo_url).arg(&target);
    log_cmd(&cmd);

    let output = cmd
        .output()
        .with_context(|| format!("cloning {} into checkout {}", repo_name, checkout_name))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!(
            "failed to clone {} into checkout {}: {}",
            repo_name,
            checkout_name,
            stderr.trim()
        );
    }

    Ok(target)
}

/// Create a full checkout with all provided repos.
///
/// Takes an iterator of (name, url, path) tuples.
/// Returns info about the created checkout.
pub fn create_checkout<'a>(
    workspace_root: &Path,
    checkout_name: &str,
    repos: impl Iterator<Item = (&'a str, &'a str, &'a str)>,
    branch: Option<&str>,
) -> Result<CheckoutInfo> {
    if checkout_exists(workspace_root, checkout_name) {
        anyhow::bail!("checkout '{}' already exists", checkout_name);
    }

    let checkout_root = checkout_path(workspace_root, checkout_name);
    std::fs::create_dir_all(&checkout_root)
        .with_context(|| format!("creating checkout root: {}", checkout_root.display()))?;

    let mut checkout_repos = Vec::new();

    for (name, url, path) in repos {
        let target = materialize_repo(workspace_root, checkout_name, name, url, path, branch)?;
        checkout_repos.push(CheckoutRepo {
            name: name.to_string(),
            path: target,
            branch: branch.map(String::from),
        });
    }

    let now = chrono::Utc::now().to_rfc3339();
    let info = CheckoutInfo {
        name: checkout_name.to_string(),
        path: checkout_root.clone(),
        repos: checkout_repos,
        created_at: now,
    };

    // Write checkout metadata
    let meta_path = checkout_root.join(".checkout.json");
    let json = serde_json::to_string_pretty(&info)?;
    std::fs::write(&meta_path, json)
        .with_context(|| format!("writing checkout metadata: {}", meta_path.display()))?;

    Ok(info)
}

/// List all checkouts under `.grip/checkouts/`.
pub fn list_checkouts(workspace_root: &Path) -> Result<Vec<CheckoutInfo>> {
    let checkouts_dir = workspace_root.join(".grip").join(CHECKOUTS_DIR);
    if !checkouts_dir.is_dir() {
        return Ok(vec![]);
    }

    let mut checkouts = Vec::new();
    for entry in std::fs::read_dir(&checkouts_dir)? {
        let entry = entry?;
        if !entry.path().is_dir() {
            continue;
        }
        let meta_path = entry.path().join(".checkout.json");
        if meta_path.is_file() {
            let content = std::fs::read_to_string(&meta_path)?;
            if let Ok(info) = serde_json::from_str::<CheckoutInfo>(&content) {
                checkouts.push(info);
            }
        } else {
            // Checkout dir exists but no metadata — construct minimal info
            let name = entry.file_name().to_string_lossy().to_string();
            checkouts.push(CheckoutInfo {
                name: name.clone(),
                path: entry.path(),
                repos: vec![],
                created_at: "unknown".to_string(),
            });
        }
    }

    Ok(checkouts)
}

/// Remove a checkout and all its contents.
pub fn remove_checkout(workspace_root: &Path, name: &str) -> Result<bool> {
    let path = checkout_path(workspace_root, name);
    if path.is_dir() {
        std::fs::remove_dir_all(&path)
            .with_context(|| format!("removing checkout: {}", path.display()))?;
        Ok(true)
    } else {
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::workspace_cache::test_support;
    use std::fs;

    fn with_cache_dir<T>(cache_dir: &Path, f: impl FnOnce() -> T) -> T {
        let _guard = test_support::ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let previous = std::env::var_os("GRIP_CACHE_DIR");
        std::env::set_var("GRIP_CACHE_DIR", cache_dir);
        let result = f();
        match previous {
            Some(value) => std::env::set_var("GRIP_CACHE_DIR", value),
            None => std::env::remove_var("GRIP_CACHE_DIR"),
        }
        result
    }

    /// Helper: create a test remote repo and bootstrap its cache
    fn setup_cached_workspace(dir: &Path) -> (PathBuf, PathBuf) {
        let remote_path = dir.join("remote-repo.git");
        let workspace = dir.join("workspace");

        // Init bare remote
        Command::new("git")
            .args(["init", "--bare"])
            .arg(&remote_path)
            .output()
            .expect("git init --bare");

        // Create work repo with a commit
        let work = dir.join("work-repo");
        Command::new("git")
            .args(["init"])
            .arg(&work)
            .output()
            .expect("git init");
        Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&work)
            .output()
            .expect("config email");
        Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&work)
            .output()
            .expect("config name");
        fs::write(work.join("README.md"), "# test repo").expect("write");
        Command::new("git")
            .args(["add", "."])
            .current_dir(&work)
            .output()
            .expect("add");
        Command::new("git")
            .args(["commit", "-m", "initial"])
            .current_dir(&work)
            .output()
            .expect("commit");
        // Push to bare remote — try both main and master
        let _ = Command::new("git")
            .args(["remote", "add", "origin"])
            .arg(&remote_path)
            .current_dir(&work)
            .output();
        let _ = Command::new("git")
            .args(["push", "origin", "HEAD"])
            .current_dir(&work)
            .output();

        // Create workspace and bootstrap cache
        fs::create_dir_all(&workspace).expect("mkdir workspace");
        let url = remote_path.to_string_lossy().to_string();
        workspace_cache::bootstrap_cache(&workspace, "testrepo", &url).expect("bootstrap cache");

        (workspace, remote_path)
    }

    #[test]
    fn test_checkout_path() {
        let root = Path::new("/ws");
        assert_eq!(
            checkout_path(root, "mybranch"),
            PathBuf::from("/ws/.grip/checkouts/mybranch")
        );
    }

    #[test]
    fn test_checkout_does_not_exist_initially() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(!checkout_exists(tmp.path(), "nope"));
    }

    #[test]
    fn test_materialize_single_repo() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let target = materialize_repo(
                &workspace,
                "test-checkout",
                "testrepo",
                &url,
                "testrepo",
                None,
            )
            .expect("materialize");

            assert!(target.join(".git").exists());
            assert!(target.join("README.md").exists());
        });
    }

    #[test]
    fn test_materialize_is_independent_clone() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let target = materialize_repo(
                &workspace,
                "independent",
                "testrepo",
                &url,
                "testrepo",
                None,
            )
            .expect("materialize");

            assert!(target.join(".git").is_dir());
            assert!(!target.join(".git").is_file());
        });
    }

    #[test]
    fn test_materialize_uses_cache_reference() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let target =
                materialize_repo(&workspace, "ref-test", "testrepo", &url, "testrepo", None)
                    .expect("materialize");

            let alternates = target.join(".git/objects/info/alternates");
            assert!(alternates.is_file(), "alternates file should exist");
            let content = fs::read_to_string(&alternates).expect("read alternates");
            assert!(
                content.contains(&workspace_cache::cache_key(&url)),
                "alternates should reference the global cache path"
            );
        });
    }

    #[test]
    fn test_create_and_list_checkout() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let repos = vec![("testrepo", url.as_str(), "testrepo")];

            let info = create_checkout(&workspace, "feat-x", repos.into_iter(), None)
                .expect("create checkout");

            assert_eq!(info.name, "feat-x");
            assert_eq!(info.repos.len(), 1);
            assert!(checkout_exists(&workspace, "feat-x"));

            let all = list_checkouts(&workspace).expect("list");
            assert_eq!(all.len(), 1);
            assert_eq!(all[0].name, "feat-x");
        });
    }

    #[test]
    fn test_create_duplicate_fails() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let repos = vec![("testrepo", url.as_str(), "testrepo")];
            create_checkout(&workspace, "dup", repos.into_iter(), None).expect("first");

            let repos2 = vec![("testrepo", url.as_str(), "testrepo")];
            let result = create_checkout(&workspace, "dup", repos2.into_iter(), None);
            assert!(result.is_err());
        });
    }

    #[test]
    fn test_remove_checkout() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let repos = vec![("testrepo", url.as_str(), "testrepo")];
            create_checkout(&workspace, "removeme", repos.into_iter(), None).expect("create");

            assert!(checkout_exists(&workspace, "removeme"));
            let removed = remove_checkout(&workspace, "removeme").expect("remove");
            assert!(removed);
            assert!(!checkout_exists(&workspace, "removeme"));
        });
    }

    #[test]
    fn test_remove_nonexistent_returns_false() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let removed = remove_checkout(tmp.path(), "nope").expect("remove");
        assert!(!removed);
    }

    #[test]
    fn test_cache_survives_checkout_removal() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let repos = vec![("testrepo", url.as_str(), "testrepo")];
            create_checkout(&workspace, "ephemeral", repos.into_iter(), None).expect("create");

            remove_checkout(&workspace, "ephemeral").expect("remove");

            assert!(
                workspace_cache::cache_exists(&workspace, "testrepo", &url).expect("cache exists"),
                "cache must survive checkout deletion"
            );
        });
    }
}

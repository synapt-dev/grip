//! Workspace checkouts — independent child clones materialized from the cache
//!
//! Each checkout lives under `.grip/checkouts/<name>/` and contains full clones
//! of manifest repos, created with `--reference` to reuse objects from the
//! bare cache. Checkouts are independently disposable.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::core::manifest::{Manifest, ManifestSettings, PlatformConfig, PlatformType, RepoConfig};
use crate::core::repo::RepoInfo;
use crate::core::workspace_cache;
use crate::util::log_cmd;

/// Directory name under .grip/ where checkouts live.
const CHECKOUTS_DIR: &str = "checkouts";

/// Filename of the per-checkout metadata file, written directly at the
/// checkout root (never inside a materialized repo's own path -- grip#775's
/// second blocker: writing a derived manifest into `.gitgrip/spaces/main`
/// collided with that being the materialized "manifest" pseudo-repo's own
/// canonical clone location when it was included in the checkout).
const CHECKOUT_METADATA_FILE: &str = ".checkout.json";

/// Metadata for a single checkout. Carries everything `manifest_from_checkout`
/// needs to reconstruct a full gripspace `Manifest` in memory -- this file is
/// the checkout's ONLY self-description; nothing else gets written into any
/// materialized repo's own directory tree.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckoutInfo {
    pub name: String,
    pub path: PathBuf,
    pub repos: Vec<CheckoutRepo>,
    pub created_at: String,
    #[serde(default)]
    pub settings: ManifestSettings,
}

/// A single repo within a checkout, carrying its already-resolved manifest
/// fields (grip#774/#775) so `manifest_from_checkout` can rebuild a faithful
/// `RepoConfig` without re-reading anything from the parent gripspace.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckoutRepo {
    pub name: String,
    /// Absolute materialized path on disk.
    pub path: PathBuf,
    pub branch: Option<String>,
    #[serde(default)]
    pub url: String,
    /// Path relative to the checkout root -- matches `materialize_repo`'s
    /// clone target exactly, so it doubles as the derived manifest's
    /// `RepoConfig.path`.
    #[serde(default)]
    pub relative_path: String,
    #[serde(default)]
    pub revision: String,
    #[serde(default)]
    pub target: String,
    #[serde(default)]
    pub sync_remote: String,
    #[serde(default)]
    pub push_remote: String,
    #[serde(default)]
    pub platform_type: PlatformType,
    #[serde(default)]
    pub platform_base_url: Option<String>,
    #[serde(default)]
    pub reference: bool,
    #[serde(default)]
    pub groups: Vec<String>,
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
/// `parent_manifest` supplies `settings` carried into the checkout metadata;
/// `repos` supplies the already-resolved per-repo info (url, path, revision,
/// target, platform, ...) both to materialize AND to record verbatim in
/// `.checkout.json` for later reconstruction via `manifest_from_checkout`.
/// Returns info about the created checkout.
pub fn create_checkout(
    workspace_root: &Path,
    checkout_name: &str,
    parent_manifest: &Manifest,
    repos: &[RepoInfo],
    branch: Option<&str>,
) -> Result<CheckoutInfo> {
    if checkout_exists(workspace_root, checkout_name) {
        anyhow::bail!("checkout '{}' already exists", checkout_name);
    }

    let checkout_root = checkout_path(workspace_root, checkout_name);
    std::fs::create_dir_all(&checkout_root)
        .with_context(|| format!("creating checkout root: {}", checkout_root.display()))?;

    let mut checkout_repos = Vec::new();

    for repo in repos {
        let target = materialize_repo(
            workspace_root,
            checkout_name,
            &repo.name,
            &repo.url,
            &repo.path,
            branch,
        )?;
        checkout_repos.push(CheckoutRepo {
            name: repo.name.clone(),
            path: target,
            branch: branch.map(String::from),
            url: repo.url.clone(),
            relative_path: repo.path.clone(),
            revision: repo.revision.clone(),
            target: repo.target.clone(),
            sync_remote: repo.sync_remote.clone(),
            push_remote: repo.push_remote.clone(),
            platform_type: repo.platform_type,
            platform_base_url: repo.platform_base_url.clone(),
            reference: repo.reference,
            groups: repo.groups.clone(),
        });
    }

    let now = chrono::Utc::now().to_rfc3339();
    let info = CheckoutInfo {
        name: checkout_name.to_string(),
        path: checkout_root.clone(),
        repos: checkout_repos,
        created_at: now,
        settings: parent_manifest.settings.clone(),
    };

    // Write checkout metadata -- the ONLY file this function writes outside
    // materialized repo directories. Nothing is written into any repo's own
    // path (grip#775 blocker 2: a derived manifest written into
    // `.gitgrip/spaces/main` collided with that being the "manifest"
    // pseudo-repo's own canonical clone location when it was included in the
    // checkout, leaving a materialized repo born dirty).
    let meta_path = checkout_root.join(CHECKOUT_METADATA_FILE);
    let json = serde_json::to_string_pretty(&info)?;
    std::fs::write(&meta_path, json)
        .with_context(|| format!("writing checkout metadata: {}", meta_path.display()))?;

    Ok(info)
}

/// Load `.checkout.json` directly from `dir` if present, without walking
/// ancestors -- the caller (the unified discovery walk in `dispatch.rs`)
/// owns the ancestor traversal so it can check every marker type at each
/// level before climbing (grip#775 blocker 1: two independent all-ancestors
/// passes let a farther `.griptree` eclipse a nearer checkout).
///
/// Distinguishes "no marker here" (`Ok(None)`, safe to keep climbing) from
/// "a marker exists but is unreadable, malformed, or structurally incomplete"
/// (`Err`, must NOT be treated as absence). Sentinel's grip#775 round-3
/// finding: collapsing both cases to `None` via `.ok()?` let a corrupted
/// `.checkout.json` fail OPEN to the parent workspace -- recreating the
/// exact silent cross-scope hazard this whole PR exists to close, just
/// through a new mechanism (a broken marker instead of a missing one).
pub fn load_checkout_metadata(dir: &Path) -> Result<Option<CheckoutInfo>> {
    let meta_path = dir.join(CHECKOUT_METADATA_FILE);
    if !meta_path.exists() {
        return Ok(None);
    }
    let content = std::fs::read_to_string(&meta_path)
        .with_context(|| format!("reading checkout metadata: {}", meta_path.display()))?;
    let info: CheckoutInfo = serde_json::from_str(&content)
        .with_context(|| format!("parsing checkout metadata: {}", meta_path.display()))?;
    validate_checkout_info(&info, &meta_path)?;
    Ok(Some(info))
}

/// Structural completeness check for metadata that parsed successfully but
/// may still be unusable -- e.g. a pre-round-2 `.checkout.json` whose
/// per-repo fields (url, relative_path, ...) all deserialize to their
/// `#[serde(default)]` empty values because those fields didn't exist yet
/// when it was written. A `RepoConfig` built from an empty url or path is
/// not a parse error, but it is not a usable workspace either.
fn validate_checkout_info(info: &CheckoutInfo, meta_path: &Path) -> Result<()> {
    for repo in &info.repos {
        if repo.url.is_empty() || repo.relative_path.is_empty() {
            anyhow::bail!(
                "checkout metadata at {} has an incomplete entry for repo '{}' (empty url or \
                 path) -- this checkout was likely created by an older gitgrip version; remove \
                 it with `gr checkout remove {}` and recreate it",
                meta_path.display(),
                repo.name,
                info.name
            );
        }
    }
    Ok(())
}

/// Reconstruct a full gripspace `Manifest` from checkout metadata, in memory
/// -- no file is ever written for this; `.checkout.json` is the single
/// source of truth a checkout carries about itself.
///
/// Validated with the SAME `Manifest::validate()` path-safety checks
/// (`path_escapes_boundary`) a YAML manifest goes through via
/// `Manifest::parse` -- a manifest built in memory is not exempt from them.
/// grip#775 round 3 (Sentinel): a nonempty but absolute or `..`-escaping
/// `CheckoutRepo.relative_path` passed `validate_checkout_info`'s
/// empty-string check yet, once joined onto the checkout root via
/// `PathBuf::join`, silently replaced it outright (`Path::join` discards the
/// base when the argument is absolute) -- redirecting authority-bearing
/// commands like `gr status` at whatever real path the metadata pointed to,
/// including an unrelated parent repo.
pub fn manifest_from_checkout(info: &CheckoutInfo) -> Result<Manifest> {
    let mut repos = HashMap::new();
    for repo in &info.repos {
        repos.insert(
            repo.name.clone(),
            RepoConfig {
                url: Some(repo.url.clone()),
                remote: None,
                path: repo.relative_path.clone(),
                revision: Some(repo.revision.clone()),
                target: Some(repo.target.clone()),
                sync_remote: Some(repo.sync_remote.clone()),
                push_remote: Some(repo.push_remote.clone()),
                copyfile: None,
                linkfile: None,
                platform: Some(PlatformConfig {
                    platform_type: repo.platform_type,
                    base_url: repo.platform_base_url.clone(),
                }),
                reference: repo.reference,
                groups: repo.groups.clone(),
                agent: None,
                clone_strategy: None,
            },
        );
    }

    let manifest = Manifest {
        version: 2,
        remotes: None,
        gripspaces: None,
        manifest: None,
        repos,
        settings: info.settings.clone(),
        workspace: None,
    };
    manifest.validate().with_context(|| {
        format!(
            "checkout '{}' has an invalid reconstructed manifest",
            info.name
        )
    })?;
    Ok(manifest)
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
        // `gr checkout list` is informational, not a scope-resolution path --
        // unlike `load_gripspace`'s discovery walk, a broken checkout here
        // should not fail the whole listing, only fall back to a minimal
        // entry so the user can see it exists and remove/recreate it.
        match load_checkout_metadata(&entry.path()) {
            Ok(Some(info)) => checkouts.push(info),
            Ok(None) | Err(_) => {
                let name = entry.file_name().to_string_lossy().to_string();
                checkouts.push(CheckoutInfo {
                    name: name.clone(),
                    path: entry.path(),
                    repos: vec![],
                    created_at: "unknown".to_string(),
                    settings: ManifestSettings::default(),
                });
            }
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
    use crate::core::manifest::ManifestSettings;
    use crate::core::workspace_cache::test_support;
    use std::fs;

    /// Build a single-repo manifest + resolved `RepoInfo`, matching the shape
    /// `setup_cached_workspace` produces (one repo named "testrepo" checked
    /// out at path "testrepo"), for tests exercising `create_checkout`'s
    /// derived-manifest path without hand-rolling every `RepoInfo` field.
    fn single_repo_manifest_and_info(
        workspace: &Path,
        name: &str,
        url: &str,
    ) -> (Manifest, Vec<RepoInfo>) {
        // parse_git_url only recognizes git@, https://, http://, and file://
        // -- setup_cached_workspace hands back a bare filesystem path, so it
        // needs the file:// scheme RepoInfo::from_config requires to resolve
        // owner/repo. materialize_repo's `git clone` accepts file:// URLs
        // the same as a bare path, so this doesn't change what gets cloned.
        let file_url = if url.contains("://") {
            url.to_string()
        } else {
            format!("file://{}", url)
        };

        let mut repos = HashMap::new();
        repos.insert(
            name.to_string(),
            RepoConfig {
                url: Some(file_url),
                remote: None,
                path: name.to_string(),
                revision: None,
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: vec![],
                agent: None,
                clone_strategy: None,
            },
        );
        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos,
            settings: ManifestSettings::default(),
            workspace: None,
        };
        let settings = manifest.settings.clone();
        let config = manifest.repos.get(name).unwrap();
        let repo_info = RepoInfo::from_config(name, config, workspace, &settings, None)
            .expect("from_config should resolve a repo with an explicit url");
        (manifest, vec![repo_info])
    }

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
            let (manifest, repos) = single_repo_manifest_and_info(&workspace, "testrepo", &url);

            let info = create_checkout(&workspace, "feat-x", &manifest, &repos, None)
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
            let (manifest, repos) = single_repo_manifest_and_info(&workspace, "testrepo", &url);
            create_checkout(&workspace, "dup", &manifest, &repos, None).expect("first");

            let result = create_checkout(&workspace, "dup", &manifest, &repos, None);
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
            let (manifest, repos) = single_repo_manifest_and_info(&workspace, "testrepo", &url);
            create_checkout(&workspace, "removeme", &manifest, &repos, None).expect("create");

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
            let (manifest, repos) = single_repo_manifest_and_info(&workspace, "testrepo", &url);
            create_checkout(&workspace, "ephemeral", &manifest, &repos, None).expect("create");

            remove_checkout(&workspace, "ephemeral").expect("remove");

            assert!(
                workspace_cache::cache_exists(&workspace, "testrepo", &url).expect("cache exists"),
                "cache must survive checkout deletion"
            );
        });
    }

    // ── grip#774/#775: checkout must be a self-describing workspace, without
    // writing into any materialized repo's own path ────────────────────────

    #[test]
    fn test_create_checkout_metadata_reconstructs_a_faithful_manifest() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let (mut manifest, repos) = single_repo_manifest_and_info(&workspace, "testrepo", &url);
            manifest.settings.pr_prefix = "[from-parent]".to_string();

            create_checkout(&workspace, "self-contained", &manifest, &repos, None)
                .expect("create checkout");

            let checkout_root = checkout_path(&workspace, "self-contained");

            let info = load_checkout_metadata(&checkout_root)
                .expect("checkout metadata should be readable")
                .expect("checkout metadata should load from the checkout root");
            let derived =
                manifest_from_checkout(&info).expect("reconstructed manifest should validate");

            assert_eq!(
                derived.repos.len(),
                1,
                "derived manifest should carry exactly the repos materialized into this checkout"
            );
            let repo_config = derived.repos.get("testrepo").expect("testrepo entry");
            assert_eq!(
                repo_config.url.as_deref(),
                Some(format!("file://{}", url).as_str()),
                "derived repo url must match the resolved (file://-scheme) url used to \
                 materialize the repo"
            );
            assert_eq!(
                repo_config.path, "testrepo",
                "derived repo path must be relative to the checkout root, matching \
                 where materialize_repo actually cloned it"
            );
            assert_eq!(
                derived.settings.pr_prefix, "[from-parent]",
                "derived manifest must carry the parent's settings, not gitgrip's built-in default"
            );
        });
    }

    #[test]
    fn test_checkout_metadata_lives_at_checkout_root_not_inside_any_repo() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());

            let url = remote.to_string_lossy().to_string();
            let (manifest, repos) = single_repo_manifest_and_info(&workspace, "testrepo", &url);
            create_checkout(&workspace, "discoverable", &manifest, &repos, None)
                .expect("create checkout");

            let checkout_root = checkout_path(&workspace, "discoverable");
            assert!(
                checkout_root.join(CHECKOUT_METADATA_FILE).is_file(),
                ".checkout.json must live directly at the checkout root"
            );
            assert!(
                load_checkout_metadata(checkout_root.join("testrepo").as_path())
                    .expect("absence is not an error")
                    .is_none(),
                "metadata must not also be discoverable from inside a materialized repo \
                 (that would mean it was written into repo content, not the checkout root)"
            );
        });
    }

    #[test]
    fn test_checkout_including_the_manifest_pseudo_repo_leaves_it_untouched() {
        // grip#775 blocker 2: the "manifest" pseudo-repo's own canonical clone
        // path IS `.gitgrip/spaces/main` (core::repo::create_manifest_repo_info).
        // A checkout that materializes a repo at that exact relative path must
        // come out of create_checkout with nothing written into it beyond what
        // `git clone` itself produced -- metadata lives only in .checkout.json
        // at the checkout root (verified above), never inside a repo's path.
        let tmp = tempfile::tempdir().expect("tempdir");
        let cache_dir = tmp.path().join("global-cache");
        with_cache_dir(&cache_dir, || {
            let (workspace, remote) = setup_cached_workspace(tmp.path());
            let url = format!("file://{}", remote.to_string_lossy());

            let manifest_pseudo_repo = RepoInfo::from_config(
                "manifest",
                &RepoConfig {
                    url: Some(url.clone()),
                    remote: None,
                    path: ".gitgrip/spaces/main".to_string(),
                    revision: None,
                    target: None,
                    sync_remote: None,
                    push_remote: None,
                    copyfile: None,
                    linkfile: None,
                    platform: None,
                    reference: false,
                    groups: vec![],
                    agent: None,
                    clone_strategy: None,
                },
                &workspace,
                &ManifestSettings::default(),
                None,
            )
            .expect("from_config should resolve the pseudo manifest repo");

            let repos = vec![manifest_pseudo_repo];
            let parent_manifest = Manifest {
                version: 2,
                remotes: None,
                gripspaces: None,
                manifest: None,
                repos: HashMap::new(),
                settings: ManifestSettings::default(),
                workspace: None,
            };

            create_checkout(
                &workspace,
                "with-manifest-repo",
                &parent_manifest,
                &repos,
                None,
            )
            .expect("create checkout including the manifest pseudo-repo");

            let checkout_root = checkout_path(&workspace, "with-manifest-repo");
            let materialized_manifest_clone =
                checkout_root.join(".gitgrip").join("spaces").join("main");
            assert!(
                materialized_manifest_clone.join(".git").is_dir(),
                "the manifest pseudo-repo should still be cloned at its configured path"
            );
            assert!(
                !materialized_manifest_clone.join("gripspace.yml").exists(),
                "nothing must be written into the materialized manifest repo's own \
                 directory -- that would corrupt its tracked content the moment the \
                 checkout is created, before any user action"
            );

            let status = Command::new("git")
                .args(["status", "--porcelain"])
                .current_dir(&materialized_manifest_clone)
                .output()
                .expect("git status");
            assert!(
                status.stdout.is_empty(),
                "materialized manifest repo clone must be born clean, got: {}",
                String::from_utf8_lossy(&status.stdout)
            );
        });
    }

    #[test]
    fn test_load_checkout_metadata_returns_none_when_missing() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(load_checkout_metadata(tmp.path())
            .expect("absence is not an error")
            .is_none());
    }

    #[test]
    fn test_load_checkout_metadata_errors_on_malformed_json_instead_of_failing_open() {
        // grip#775 round 3 (Sentinel): a marker that EXISTS but is broken must
        // never be treated the same as no marker at all -- that collapse is
        // exactly what let a corrupted `.checkout.json` fail open to the
        // parent workspace. This replaces the old test that pinned the
        // unsafe `None`-on-malformed-JSON behavior as if it were correct.
        let tmp = tempfile::tempdir().expect("tempdir");
        fs::write(tmp.path().join(CHECKOUT_METADATA_FILE), "not valid json").unwrap();
        let result = load_checkout_metadata(tmp.path());
        assert!(
            result.is_err(),
            "a marker that exists but fails to parse must be an error, not Ok(None) -- \
             collapsing the two lets the discovery walk silently climb past a broken \
             checkout to the parent workspace"
        );
    }

    #[test]
    fn test_load_checkout_metadata_errors_on_legacy_metadata_with_empty_repo_fields() {
        // grip#775 round 3 requirement 3: metadata that parses successfully
        // but whose per-repo fields deserialize to their #[serde(default)]
        // empty values (e.g. a pre-round-2 .checkout.json written before url/
        // relative_path existed on CheckoutRepo) is not a parse error, but a
        // RepoConfig built from an empty url or path is unusable.
        let tmp = tempfile::tempdir().expect("tempdir");
        let legacy_json = r#"{
            "name": "legacy",
            "path": "/tmp/legacy",
            "created_at": "2026-01-01T00:00:00Z",
            "repos": [
                {"name": "app", "path": "/tmp/legacy/app", "branch": null}
            ]
        }"#;
        fs::write(tmp.path().join(CHECKOUT_METADATA_FILE), legacy_json).unwrap();
        let result = load_checkout_metadata(tmp.path());
        assert!(
            result.is_err(),
            "metadata with an empty url/relative_path must fail structural validation, \
             not silently reconstruct a Manifest with an unusable repo config"
        );
    }
}

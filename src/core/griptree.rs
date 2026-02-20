//! Griptree (worktree) management
//!
//! Griptrees are isolated parallel workspaces for different branches.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use thiserror::Error;

/// Errors that can occur with griptree operations
#[derive(Error, Debug)]
pub enum GriptreeError {
    #[error("Failed to read griptree config: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Failed to parse griptree config: {0}")]
    ParseError(#[from] serde_json::Error),

    #[error("Griptree is locked: {0}")]
    Locked(String),

    #[error("Griptree not found: {0}")]
    NotFound(String),

    #[error("Invalid upstream reference: {0}. Expected format: <remote>/<branch>")]
    InvalidUpstream(String),
}

/// Griptree status
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GriptreeStatus {
    /// Active and in use
    Active,
    /// Branch was deleted, griptree is orphaned
    Orphan,
    /// Legacy griptree (pre-config format)
    Legacy,
}

/// Griptree configuration (stored in .gitgrip/griptrees/<branch>/config.json)
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GriptreeConfig {
    /// Branch name this griptree is for
    pub branch: String,
    /// Absolute path to griptree directory
    pub path: String,
    /// ISO timestamp when created
    pub created_at: DateTime<Utc>,
    /// User who created it
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_by: Option<String>,
    /// Prevents accidental removal
    #[serde(default)]
    pub locked: bool,
    /// ISO timestamp when locked
    #[serde(skip_serializing_if = "Option::is_none")]
    pub locked_at: Option<DateTime<Utc>>,
    /// Reason for locking
    #[serde(skip_serializing_if = "Option::is_none")]
    pub locked_reason: Option<String>,
    /// Per-repo upstream branch mapping (e.g., origin/main, origin/dev)
    #[serde(default, skip_serializing_if = "HashMap::is_empty")]
    pub repo_upstreams: HashMap<String, String>,
}

impl GriptreeConfig {
    /// Create a new griptree config
    pub fn new(branch: &str, path: &str) -> Self {
        Self {
            branch: branch.to_string(),
            path: path.to_string(),
            created_at: Utc::now(),
            created_by: std::env::var("USER").ok(),
            locked: false,
            locked_at: None,
            locked_reason: None,
            repo_upstreams: HashMap::new(),
        }
    }

    /// Load config from a file
    pub fn load(path: &PathBuf) -> Result<Self, GriptreeError> {
        let content = std::fs::read_to_string(path)?;
        let config: GriptreeConfig = serde_json::from_str(&content)?;
        Ok(config)
    }

    /// Save config to a file
    pub fn save(&self, path: &PathBuf) -> Result<(), GriptreeError> {
        let json = serde_json::to_string_pretty(self)?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, json)?;
        Ok(())
    }

    /// Load griptree config from a workspace root (if present)
    pub fn load_from_workspace(workspace_root: &PathBuf) -> Result<Option<Self>, GriptreeError> {
        let path = workspace_root.join(".gitgrip").join("griptree.json");
        if !path.exists() {
            return Ok(None);
        }
        Self::load(&path).map(Some)
    }

    /// Resolve upstream branch for a repo, falling back to origin/<revision>
    pub fn upstream_for_repo(
        &self,
        repo_name: &str,
        revision: &str,
    ) -> Result<String, GriptreeError> {
        let upstream = self
            .repo_upstreams
            .get(repo_name)
            .cloned()
            .unwrap_or_else(|| format!("origin/{}", revision));
        Self::validate_upstream_ref(&upstream)?;
        Ok(upstream)
    }

    fn validate_upstream_ref(upstream: &str) -> Result<(), GriptreeError> {
        let mut parts = upstream.splitn(2, '/');
        let remote = parts.next().unwrap_or("").trim();
        let branch = parts.next().unwrap_or("").trim();
        if remote.is_empty() || branch.is_empty() {
            return Err(GriptreeError::InvalidUpstream(upstream.to_string()));
        }
        Ok(())
    }

    /// Lock the griptree
    pub fn lock(&mut self, reason: Option<&str>) {
        self.locked = true;
        self.locked_at = Some(Utc::now());
        self.locked_reason = reason.map(|s| s.to_string());
    }

    /// Unlock the griptree
    pub fn unlock(&mut self) {
        self.locked = false;
        self.locked_at = None;
        self.locked_reason = None;
    }
}

/// Pointer file stored in the griptree directory (.griptree)
/// This file indicates that the current directory is a griptree and points
/// back to the main workspace.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GriptreePointer {
    /// Absolute path to main workspace
    pub main_workspace: String,
    /// Branch name
    pub branch: String,
    /// Whether the griptree is locked (optional for backwards compat)
    #[serde(default)]
    pub locked: bool,
    /// When the griptree was created (optional for backwards compat)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<DateTime<Utc>>,
    /// Track original branch for each repo (for merge back to main)
    #[serde(default)]
    pub repos: Vec<GriptreeRepoInfo>,
    /// Manifest branch for this griptree (optional for backwards compat)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub manifest_branch: Option<String>,
    /// Manifest worktree name (for cleanup)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub manifest_worktree_name: Option<String>,
}

/// Per-repo griptree info (tracked in pointer file)
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GriptreeRepoInfo {
    /// Repository name
    pub name: String,
    /// Original branch name (before griptree creation)
    pub original_branch: String,
    /// Whether this is a reference repo
    pub is_reference: bool,
    /// The name passed to git worktree add (for cleanup)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub worktree_name: Option<String>,
    /// Absolute path to the worktree in the griptree
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub worktree_path: Option<String>,
    /// Absolute path to the main repo (for worktree cleanup)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub main_repo_path: Option<String>,
}

impl GriptreePointer {
    /// Load pointer from a .griptree file
    pub fn load(path: &std::path::Path) -> Result<Self, GriptreeError> {
        let content = std::fs::read_to_string(path)?;
        let pointer: GriptreePointer = serde_json::from_str(&content)?;
        Ok(pointer)
    }

    /// Find a .griptree pointer file by searching current and parent directories
    pub fn find_in_ancestors(start: &std::path::Path) -> Option<(std::path::PathBuf, Self)> {
        let mut current = start.to_path_buf();
        loop {
            let pointer_path = current.join(".griptree");
            if pointer_path.exists() {
                if let Ok(pointer) = Self::load(&pointer_path) {
                    return Some((current, pointer));
                }
            }

            match current.parent() {
                Some(parent) => current = parent.to_path_buf(),
                None => return None,
            }
        }
    }
}

/// Per-repo worktree info
#[derive(Debug, Clone)]
pub struct TreeRepoInfo {
    /// Repository name
    pub name: String,
    /// Worktree path
    pub path: PathBuf,
    /// Branch name
    pub branch: String,
    /// Worktree exists
    pub exists: bool,
}

/// Full griptree information
#[derive(Debug, Clone)]
pub struct TreeInfo {
    /// Branch name
    pub branch: String,
    /// Griptree path
    pub path: PathBuf,
    /// Whether it's locked
    pub locked: bool,
    /// Per-repo worktree info
    pub repos: Vec<TreeRepoInfo>,
    /// Griptree status
    pub status: Option<GriptreeStatus>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_griptree_config() {
        let config = GriptreeConfig::new("feat/test", "/path/to/griptree");
        assert_eq!(config.branch, "feat/test");
        assert_eq!(config.path, "/path/to/griptree");
        assert!(!config.locked);
    }

    #[test]
    fn test_lock_unlock() {
        let mut config = GriptreeConfig::new("feat/test", "/path");

        config.lock(Some("Important work in progress"));
        assert!(config.locked);
        assert!(config.locked_at.is_some());
        assert_eq!(
            config.locked_reason,
            Some("Important work in progress".to_string())
        );

        config.unlock();
        assert!(!config.locked);
        assert!(config.locked_at.is_none());
        assert!(config.locked_reason.is_none());
    }

    #[test]
    fn test_serialize_griptree_config() {
        let config = GriptreeConfig::new("main", "/workspace");
        let json = serde_json::to_string(&config).unwrap();
        assert!(json.contains("\"branch\":\"main\""));
    }

    #[test]
    fn test_upstream_for_repo_fallback_and_override() {
        let mut config = GriptreeConfig::new("feat/test", "/path");
        assert_eq!(
            config.upstream_for_repo("repo", "main").unwrap(),
            "origin/main".to_string()
        );

        config
            .repo_upstreams
            .insert("repo".to_string(), "origin/dev".to_string());
        assert_eq!(
            config.upstream_for_repo("repo", "main").unwrap(),
            "origin/dev".to_string()
        );
    }

    #[test]
    fn test_validate_upstream_ref_invalid() {
        assert!(GriptreeConfig::validate_upstream_ref("nobranch").is_err());
        assert!(GriptreeConfig::validate_upstream_ref("").is_err());
        assert!(GriptreeConfig::validate_upstream_ref("/").is_err());
    }

    #[test]
    fn test_validate_upstream_ref_valid() {
        assert!(GriptreeConfig::validate_upstream_ref("origin/main").is_ok());
        assert!(GriptreeConfig::validate_upstream_ref("upstream/feat/x").is_ok());
    }

    #[test]
    fn test_save_and_load() {
        let temp = tempfile::TempDir::new().unwrap();
        let config_path = temp.path().join("config.json");

        let mut config = GriptreeConfig::new("feat/save-test", "/workspace");
        config
            .repo_upstreams
            .insert("myrepo".to_string(), "origin/dev".to_string());

        config.save(&config_path).unwrap();
        assert!(config_path.exists());

        let loaded = GriptreeConfig::load(&config_path).unwrap();
        assert_eq!(loaded.branch, "feat/save-test");
        assert_eq!(loaded.path, "/workspace");
        assert_eq!(loaded.repo_upstreams.get("myrepo").unwrap(), "origin/dev");
    }

    #[test]
    fn test_load_nonexistent_file() {
        let result = GriptreeConfig::load(&PathBuf::from("/nonexistent/config.json"));
        assert!(result.is_err());
    }

    #[test]
    fn test_load_invalid_json() {
        let temp = tempfile::TempDir::new().unwrap();
        let config_path = temp.path().join("bad.json");
        std::fs::write(&config_path, "not valid json").unwrap();

        let result = GriptreeConfig::load(&config_path);
        assert!(result.is_err());
    }

    #[test]
    fn test_load_from_workspace_none_when_missing() {
        let temp = tempfile::TempDir::new().unwrap();
        let result = GriptreeConfig::load_from_workspace(&temp.path().to_path_buf()).unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn test_load_from_workspace_some_when_present() {
        let temp = tempfile::TempDir::new().unwrap();
        let gitgrip_dir = temp.path().join(".gitgrip");
        std::fs::create_dir_all(&gitgrip_dir).unwrap();

        let config = GriptreeConfig::new("feat/ws", temp.path().to_str().unwrap());
        let config_path = gitgrip_dir.join("griptree.json");
        config.save(&config_path).unwrap();

        let loaded = GriptreeConfig::load_from_workspace(&temp.path().to_path_buf())
            .unwrap()
            .expect("should find config");
        assert_eq!(loaded.branch, "feat/ws");
    }

    #[test]
    fn test_griptree_pointer_load() {
        let temp = tempfile::TempDir::new().unwrap();
        let pointer_path = temp.path().join(".griptree");

        let pointer = GriptreePointer {
            main_workspace: "/main/workspace".to_string(),
            branch: "feat/pointer-test".to_string(),
            locked: false,
            created_at: Some(Utc::now()),
            repos: vec![GriptreeRepoInfo {
                name: "myrepo".to_string(),
                original_branch: "main".to_string(),
                is_reference: false,
                worktree_name: None,
                worktree_path: None,
                main_repo_path: None,
            }],
            manifest_branch: None,
            manifest_worktree_name: None,
        };

        let json = serde_json::to_string_pretty(&pointer).unwrap();
        std::fs::write(&pointer_path, json).unwrap();

        let loaded = GriptreePointer::load(&pointer_path).unwrap();
        assert_eq!(loaded.branch, "feat/pointer-test");
        assert_eq!(loaded.main_workspace, "/main/workspace");
        assert_eq!(loaded.repos.len(), 1);
        assert_eq!(loaded.repos[0].name, "myrepo");
    }

    #[test]
    fn test_griptree_pointer_load_invalid() {
        let temp = tempfile::TempDir::new().unwrap();
        let pointer_path = temp.path().join(".griptree");
        std::fs::write(&pointer_path, "bad json").unwrap();

        let result = GriptreePointer::load(&pointer_path);
        assert!(result.is_err());
    }

    #[test]
    fn test_griptree_pointer_find_in_ancestors() {
        let temp = tempfile::TempDir::new().unwrap();
        let nested = temp.path().join("a").join("b").join("c");
        std::fs::create_dir_all(&nested).unwrap();

        // Place pointer at root
        let pointer = GriptreePointer {
            main_workspace: "/main".to_string(),
            branch: "feat/ancestor".to_string(),
            locked: false,
            created_at: None,
            repos: vec![],
            manifest_branch: None,
            manifest_worktree_name: None,
        };
        let json = serde_json::to_string(&pointer).unwrap();
        std::fs::write(temp.path().join(".griptree"), json).unwrap();

        let found = GriptreePointer::find_in_ancestors(&nested);
        assert!(found.is_some());
        let (found_path, found_pointer) = found.unwrap();
        assert_eq!(found_path, temp.path());
        assert_eq!(found_pointer.branch, "feat/ancestor");
    }
}

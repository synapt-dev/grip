//! Pre-sync snapshot for rollback support
//!
//! Before `gr sync` pulls changes, [`SyncSnapshot::capture`] records each
//! repo's HEAD commit and branch. If something goes wrong, `gr sync --rollback`
//! restores every repo to the recorded state.
//!
//! State is persisted to `.gitgrip/sync-state.json`.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

use crate::core::repo::RepoInfo;
use crate::git::{get_current_branch, open_repo, GitError};

/// Snapshot of the entire workspace taken before a sync.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncSnapshot {
    /// When the snapshot was taken.
    pub timestamp: DateTime<Utc>,
    /// Per-repo state.
    pub repos: Vec<RepoSnapshot>,
}

/// Per-repo state recorded before sync.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoSnapshot {
    /// Repository name (matches manifest key).
    pub name: String,
    /// Absolute path at capture time.
    pub path: PathBuf,
    /// HEAD commit SHA.
    pub head_commit: String,
    /// Branch that was checked out.
    pub branch: String,
}

const STATE_FILE: &str = ".gitgrip/sync-state.json";

impl SyncSnapshot {
    /// Capture the current state of all repos that exist on disk.
    pub fn capture(_workspace_root: &Path, repos: &[RepoInfo]) -> Result<Self, GitError> {
        let mut snapshots = Vec::new();

        for repo in repos {
            if !repo.absolute_path.exists() {
                continue;
            }

            let git_repo = match open_repo(&repo.absolute_path) {
                Ok(r) => r,
                Err(_) => continue, // skip repos we can't open
            };

            let branch = get_current_branch(&git_repo).unwrap_or_default();

            let head = git_repo
                .head()
                .ok()
                .and_then(|h| h.target())
                .map(|oid| oid.to_string())
                .unwrap_or_default();

            if head.is_empty() {
                continue; // skip repos with no commits
            }

            snapshots.push(RepoSnapshot {
                name: repo.name.clone(),
                path: repo.absolute_path.clone(),
                branch,
                head_commit: head,
            });
        }

        Ok(Self {
            timestamp: Utc::now(),
            repos: snapshots,
        })
    }

    /// Persist the snapshot to `.gitgrip/sync-state.json`.
    pub fn save(&self, workspace_root: &Path) -> Result<(), GitError> {
        let path = workspace_root.join(STATE_FILE);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(GitError::Io)?;
        }
        let json = serde_json::to_string_pretty(self)
            .map_err(|e| GitError::OperationFailed(format!("serialize sync state: {}", e)))?;
        std::fs::write(&path, json).map_err(GitError::Io)?;
        Ok(())
    }

    /// Load the most recent snapshot, if any.
    pub fn load_latest(workspace_root: &Path) -> Result<Option<Self>, GitError> {
        let path = workspace_root.join(STATE_FILE);
        if !path.exists() {
            return Ok(None);
        }
        let contents = std::fs::read_to_string(&path).map_err(GitError::Io)?;
        let snapshot: Self = serde_json::from_str(&contents)
            .map_err(|e| GitError::OperationFailed(format!("parse sync state: {}", e)))?;
        Ok(Some(snapshot))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    fn setup_repo(dir: &Path) {
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
    }

    fn head_sha(dir: &Path) -> String {
        let output = Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(dir)
            .output()
            .unwrap();
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    #[test]
    fn test_capture_and_save_load() {
        let workspace = TempDir::new().unwrap();
        let repo_dir = workspace.path().join("repo-a");
        fs::create_dir_all(&repo_dir).unwrap();
        setup_repo(&repo_dir);

        let repos = vec![RepoInfo {
            name: "repo-a".to_string(),
            url: "https://github.com/test/repo-a.git".to_string(),
            path: "./repo-a".to_string(),
            absolute_path: repo_dir.clone(),
            default_branch: "main".to_string(),
            target_ref: "origin/main".to_string(),
            owner: "test".to_string(),
            repo: "repo-a".to_string(),
            platform_type: crate::core::manifest::PlatformType::GitHub,
            platform_base_url: None,
            project: None,
            reference: false,
            groups: vec![],
            agent: None,
        }];

        let snapshot = SyncSnapshot::capture(workspace.path(), &repos).unwrap();
        assert_eq!(snapshot.repos.len(), 1);
        assert_eq!(snapshot.repos[0].name, "repo-a");
        assert_eq!(snapshot.repos[0].branch, "main");
        assert_eq!(snapshot.repos[0].head_commit, head_sha(&repo_dir));

        // Save and reload
        snapshot.save(workspace.path()).unwrap();
        let loaded = SyncSnapshot::load_latest(workspace.path())
            .unwrap()
            .unwrap();
        assert_eq!(loaded.repos.len(), 1);
        assert_eq!(loaded.repos[0].head_commit, snapshot.repos[0].head_commit);
    }

    #[test]
    fn test_load_latest_no_file() {
        let workspace = TempDir::new().unwrap();
        let result = SyncSnapshot::load_latest(workspace.path()).unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn test_capture_skips_missing_repos() {
        let workspace = TempDir::new().unwrap();
        let repos = vec![RepoInfo {
            name: "missing".to_string(),
            url: "https://github.com/test/missing.git".to_string(),
            path: "./missing".to_string(),
            absolute_path: workspace.path().join("missing"),
            default_branch: "main".to_string(),
            target_ref: "origin/main".to_string(),
            owner: "test".to_string(),
            repo: "missing".to_string(),
            platform_type: crate::core::manifest::PlatformType::GitHub,
            platform_base_url: None,
            project: None,
            reference: false,
            groups: vec![],
            agent: None,
        }];

        let snapshot = SyncSnapshot::capture(workspace.path(), &repos).unwrap();
        assert!(snapshot.repos.is_empty());
    }
}

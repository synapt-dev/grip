//! State file management
//!
//! The state file (.gitgrip/state.json) tracks persistent state across commands,
//! including PR links and branch-to-PR mappings.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;
use thiserror::Error;

use crate::platform::types::{CheckStatusDetails, PRState, PlatformType};

/// Errors that can occur when loading or saving state
#[derive(Error, Debug)]
pub enum StateError {
    #[error("Failed to read state file: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Failed to parse state JSON: {0}")]
    ParseError(#[from] serde_json::Error),
}

/// A linked PR in a repository
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LinkedPR {
    /// Repository name (from manifest)
    pub repo_name: String,
    /// Owner/namespace from git URL
    pub owner: String,
    /// Repo name from git URL
    pub repo: String,
    /// PR number
    pub number: u64,
    /// PR URL
    pub url: String,
    /// PR state
    pub state: PRState,
    /// Has approval
    pub approved: bool,
    /// CI/CD checks passed
    pub checks_pass: bool,
    /// Can be merged
    pub mergeable: bool,
    /// Hosting platform type
    #[serde(skip_serializing_if = "Option::is_none")]
    pub platform_type: Option<PlatformType>,
    /// Detailed check status
    #[serde(skip_serializing_if = "Option::is_none")]
    pub check_details: Option<CheckStatusDetails>,
}

/// The persistent state file structure
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StateFile {
    /// Current manifest PR being worked on
    #[serde(skip_serializing_if = "Option::is_none")]
    pub current_manifest_pr: Option<u64>,
    /// Map: branch name -> manifest PR number
    #[serde(default)]
    pub branch_to_pr: HashMap<String, u64>,
    /// Map: manifest PR number -> linked PRs
    #[serde(default)]
    pub pr_links: HashMap<String, Vec<LinkedPR>>,
}

impl StateFile {
    /// Load state from a JSON file
    pub fn load<P: AsRef<Path>>(path: P) -> Result<Self, StateError> {
        let path = path.as_ref();
        if !path.exists() {
            return Ok(Self::default());
        }
        let content = std::fs::read_to_string(path)?;
        Self::parse(&content)
    }

    /// Parse state from a JSON string
    pub fn parse(json: &str) -> Result<Self, StateError> {
        let state: StateFile = serde_json::from_str(json)?;
        Ok(state)
    }

    /// Save state to a JSON file
    pub fn save<P: AsRef<Path>>(&self, path: P) -> Result<(), StateError> {
        let json = serde_json::to_string_pretty(self)?;

        // Ensure parent directory exists
        if let Some(parent) = path.as_ref().parent() {
            std::fs::create_dir_all(parent)?;
        }

        std::fs::write(path, json)?;
        Ok(())
    }

    /// Get the PR number for a branch
    pub fn get_pr_for_branch(&self, branch: &str) -> Option<u64> {
        self.branch_to_pr.get(branch).copied()
    }

    /// Set the PR number for a branch
    pub fn set_pr_for_branch(&mut self, branch: &str, pr_number: u64) {
        self.branch_to_pr.insert(branch.to_string(), pr_number);
    }

    /// Get linked PRs for a manifest PR
    pub fn get_linked_prs(&self, manifest_pr: u64) -> Option<&Vec<LinkedPR>> {
        self.pr_links.get(&manifest_pr.to_string())
    }

    /// Set linked PRs for a manifest PR
    pub fn set_linked_prs(&mut self, manifest_pr: u64, links: Vec<LinkedPR>) {
        self.pr_links.insert(manifest_pr.to_string(), links);
    }

    /// Add a linked PR to a manifest PR
    pub fn add_linked_pr(&mut self, manifest_pr: u64, link: LinkedPR) {
        let key = manifest_pr.to_string();
        self.pr_links.entry(key).or_default().push(link);
    }

    /// Update a linked PR's status
    pub fn update_linked_pr<F>(&mut self, manifest_pr: u64, repo_name: &str, update_fn: F)
    where
        F: FnOnce(&mut LinkedPR),
    {
        let key = manifest_pr.to_string();
        if let Some(links) = self.pr_links.get_mut(&key) {
            if let Some(link) = links.iter_mut().find(|l| l.repo_name == repo_name) {
                update_fn(link);
            }
        }
    }

    /// Remove all state for a branch
    pub fn remove_branch(&mut self, branch: &str) {
        if let Some(pr_number) = self.branch_to_pr.remove(branch) {
            self.pr_links.remove(&pr_number.to_string());
        }
    }

    /// Check if all linked PRs are ready to merge
    pub fn all_linked_prs_ready(&self, manifest_pr: u64) -> bool {
        if let Some(links) = self.get_linked_prs(manifest_pr) {
            links.iter().all(|link| {
                link.state == PRState::Open && link.approved && link.checks_pass && link.mergeable
            })
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_state() {
        let state = StateFile::default();
        assert!(state.current_manifest_pr.is_none());
        assert!(state.branch_to_pr.is_empty());
        assert!(state.pr_links.is_empty());
    }

    #[test]
    fn test_parse_state() {
        let json = r#"{
            "currentManifestPr": 42,
            "branchToPr": {
                "feat/new-feature": 42
            },
            "prLinks": {
                "42": [
                    {
                        "repoName": "app",
                        "owner": "user",
                        "repo": "app",
                        "number": 123,
                        "url": "https://github.com/user/app/pull/123",
                        "state": "open",
                        "approved": true,
                        "checksPass": true,
                        "mergeable": true
                    }
                ]
            }
        }"#;

        let state = StateFile::parse(json).unwrap();
        assert_eq!(state.current_manifest_pr, Some(42));
        assert_eq!(state.get_pr_for_branch("feat/new-feature"), Some(42));

        let links = state.get_linked_prs(42).unwrap();
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].repo_name, "app");
        assert_eq!(links[0].number, 123);
    }

    #[test]
    fn test_branch_pr_mapping() {
        let mut state = StateFile::default();

        state.set_pr_for_branch("feat/test", 100);
        assert_eq!(state.get_pr_for_branch("feat/test"), Some(100));

        state.remove_branch("feat/test");
        assert!(state.get_pr_for_branch("feat/test").is_none());
    }

    #[test]
    fn test_all_linked_prs_ready() {
        let mut state = StateFile::default();

        let link = LinkedPR {
            repo_name: "app".to_string(),
            owner: "user".to_string(),
            repo: "app".to_string(),
            number: 1,
            url: "https://github.com/user/app/pull/1".to_string(),
            state: PRState::Open,
            approved: true,
            checks_pass: true,
            mergeable: true,
            platform_type: None,
            check_details: None,
        };

        state.add_linked_pr(42, link);
        assert!(state.all_linked_prs_ready(42));
    }

    #[test]
    fn test_all_linked_prs_not_ready_when_not_approved() {
        let mut state = StateFile::default();
        let link = LinkedPR {
            repo_name: "app".to_string(),
            owner: "user".to_string(),
            repo: "app".to_string(),
            number: 1,
            url: "https://github.com/user/app/pull/1".to_string(),
            state: PRState::Open,
            approved: false,
            checks_pass: true,
            mergeable: true,
            platform_type: None,
            check_details: None,
        };
        state.add_linked_pr(42, link);
        assert!(!state.all_linked_prs_ready(42));
    }

    #[test]
    fn test_all_linked_prs_not_ready_when_checks_fail() {
        let mut state = StateFile::default();
        let link = LinkedPR {
            repo_name: "app".to_string(),
            owner: "user".to_string(),
            repo: "app".to_string(),
            number: 1,
            url: "url".to_string(),
            state: PRState::Open,
            approved: true,
            checks_pass: false,
            mergeable: true,
            platform_type: None,
            check_details: None,
        };
        state.add_linked_pr(42, link);
        assert!(!state.all_linked_prs_ready(42));
    }

    #[test]
    fn test_all_linked_prs_not_ready_when_merged() {
        let mut state = StateFile::default();
        let link = LinkedPR {
            repo_name: "app".to_string(),
            owner: "user".to_string(),
            repo: "app".to_string(),
            number: 1,
            url: "url".to_string(),
            state: PRState::Merged,
            approved: true,
            checks_pass: true,
            mergeable: true,
            platform_type: None,
            check_details: None,
        };
        state.add_linked_pr(42, link);
        assert!(!state.all_linked_prs_ready(42));
    }

    #[test]
    fn test_all_linked_prs_not_ready_no_links() {
        let state = StateFile::default();
        assert!(!state.all_linked_prs_ready(999));
    }

    #[test]
    fn test_update_linked_pr() {
        let mut state = StateFile::default();
        let link = LinkedPR {
            repo_name: "app".to_string(),
            owner: "user".to_string(),
            repo: "app".to_string(),
            number: 1,
            url: "url".to_string(),
            state: PRState::Open,
            approved: false,
            checks_pass: false,
            mergeable: false,
            platform_type: None,
            check_details: None,
        };
        state.add_linked_pr(42, link);

        state.update_linked_pr(42, "app", |pr| {
            pr.approved = true;
            pr.checks_pass = true;
            pr.mergeable = true;
        });

        let links = state.get_linked_prs(42).unwrap();
        assert!(links[0].approved);
        assert!(links[0].checks_pass);
        assert!(links[0].mergeable);
    }

    #[test]
    fn test_remove_branch_cleans_pr_links() {
        let mut state = StateFile::default();
        state.set_pr_for_branch("feat/test", 100);
        state.set_linked_prs(
            100,
            vec![LinkedPR {
                repo_name: "app".to_string(),
                owner: "user".to_string(),
                repo: "app".to_string(),
                number: 1,
                url: "url".to_string(),
                state: PRState::Open,
                approved: true,
                checks_pass: true,
                mergeable: true,
                platform_type: None,
                check_details: None,
            }],
        );

        state.remove_branch("feat/test");
        assert!(state.get_pr_for_branch("feat/test").is_none());
        assert!(state.get_linked_prs(100).is_none());
    }

    #[test]
    fn test_save_and_load() {
        let temp = tempfile::TempDir::new().unwrap();
        let path = temp.path().join("state.json");

        let mut state = StateFile::default();
        state.set_pr_for_branch("feat/x", 42);
        state.save(&path).unwrap();

        let loaded = StateFile::load(&path).unwrap();
        assert_eq!(loaded.get_pr_for_branch("feat/x"), Some(42));
    }

    #[test]
    fn test_load_nonexistent_returns_default() {
        let state = StateFile::load("/nonexistent/path/state.json").unwrap();
        assert!(state.branch_to_pr.is_empty());
    }
}

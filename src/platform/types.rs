//! Shared types for hosting platforms

use serde::{Deserialize, Serialize};

pub use crate::core::manifest::PlatformType;

/// Pull request state
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum PRState {
    #[default]
    Open,
    Closed,
    Merged,
}

impl std::fmt::Display for PRState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PRState::Open => write!(f, "open"),
            PRState::Closed => write!(f, "closed"),
            PRState::Merged => write!(f, "merged"),
        }
    }
}

/// PR head reference information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PRHead {
    /// Branch reference name
    #[serde(rename = "ref")]
    pub ref_name: String,
    /// Commit SHA
    pub sha: String,
}

/// PR base reference information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PRBase {
    /// Branch reference name
    #[serde(rename = "ref")]
    pub ref_name: String,
}

/// Normalized pull request data across platforms
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PullRequest {
    /// PR number
    pub number: u64,
    /// PR URL
    pub url: String,
    /// PR title
    pub title: String,
    /// PR body/description
    pub body: String,
    /// PR state
    pub state: PRState,
    /// Whether the PR has been merged
    pub merged: bool,
    /// Whether the PR can be merged (null if unknown)
    pub mergeable: Option<bool>,
    /// Head branch info
    pub head: PRHead,
    /// Base branch info
    pub base: PRBase,
}

/// Options for creating a PR
#[derive(Debug, Clone, Default)]
pub struct PRCreateOptions {
    /// PR title
    pub title: String,
    /// PR body/description
    pub body: Option<String>,
    /// Base branch (target)
    pub base: Option<String>,
    /// Create as draft PR
    pub draft: Option<bool>,
}

/// Merge method for PRs
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, clap::ValueEnum)]
pub enum MergeMethod {
    #[default]
    Merge,
    Squash,
    Rebase,
}

impl std::fmt::Display for MergeMethod {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MergeMethod::Merge => write!(f, "merge"),
            MergeMethod::Squash => write!(f, "squash"),
            MergeMethod::Rebase => write!(f, "rebase"),
        }
    }
}

/// Options for merging a PR
#[derive(Debug, Clone, Default)]
pub struct PRMergeOptions {
    /// Merge method
    pub method: Option<MergeMethod>,
    /// Delete branch after merge
    pub delete_branch: Option<bool>,
}

/// Filter options for listing PRs
#[derive(Debug, Clone, Default)]
pub struct PRListFilter {
    /// Filter by state (default: open)
    pub state: Option<PRState>,
    /// Maximum number of PRs to return
    pub limit: Option<u32>,
}

/// Result of creating a PR
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PRCreateResult {
    /// PR number
    pub number: u64,
    /// PR URL
    pub url: String,
}

/// PR review information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PRReview {
    /// Review state (e.g., "APPROVED", "CHANGES_REQUESTED")
    pub state: String,
    /// Reviewer username
    pub user: String,
}

/// Review event type for creating reviews
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, clap::ValueEnum)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ReviewEvent {
    /// Approve the PR
    Approve,
    /// Request changes
    RequestChanges,
    /// Leave a comment without approval/rejection
    Comment,
}

/// Status check state
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum CheckState {
    #[default]
    Pending,
    Success,
    Failure,
}

impl std::fmt::Display for CheckState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CheckState::Pending => write!(f, "pending"),
            CheckState::Success => write!(f, "success"),
            CheckState::Failure => write!(f, "failure"),
        }
    }
}

/// Individual status check
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusCheck {
    /// Check context/name
    pub context: String,
    /// Check state
    pub state: String,
}

/// Combined status check result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusCheckResult {
    /// Overall state
    pub state: CheckState,
    /// Individual statuses
    pub statuses: Vec<StatusCheck>,
}

/// Detailed check status information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckStatusDetails {
    /// Overall state
    pub state: CheckState,
    /// Number of passed checks
    pub passed: u32,
    /// Number of failed checks
    pub failed: u32,
    /// Number of pending checks
    pub pending: u32,
    /// Number of skipped checks
    pub skipped: u32,
    /// Total number of checks
    pub total: u32,
}

/// Allowed merge methods for a repository
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AllowedMergeMethods {
    /// Allow merge commits
    pub merge: bool,
    /// Allow squash merges
    pub squash: bool,
    /// Allow rebase merges
    pub rebase: bool,
}

impl Default for AllowedMergeMethods {
    fn default() -> Self {
        Self {
            merge: true,
            squash: true,
            rebase: true,
        }
    }
}

/// Parsed repository information from URL
#[derive(Debug, Clone)]
pub struct ParsedRepoInfo {
    /// Owner/namespace
    pub owner: String,
    /// Repository name
    pub repo: String,
    /// Project name (Azure DevOps only)
    pub project: Option<String>,
    /// Detected platform type
    pub platform: Option<PlatformType>,
}

/// Result of creating a GitHub/platform release
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReleaseResult {
    /// Release ID
    pub id: u64,
    /// Git tag name
    pub tag: String,
    /// Release URL
    pub url: String,
}

/// Issue state
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum IssueState {
    #[default]
    Open,
    Closed,
}

impl std::fmt::Display for IssueState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            IssueState::Open => write!(f, "open"),
            IssueState::Closed => write!(f, "closed"),
        }
    }
}

/// Issue label
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IssueLabel {
    /// Label name
    pub name: String,
    /// Label color (hex)
    pub color: Option<String>,
}

/// Normalized issue data across platforms
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Issue {
    /// Issue number
    pub number: u64,
    /// Issue URL
    pub url: String,
    /// Issue title
    pub title: String,
    /// Issue body/description
    pub body: String,
    /// Issue state
    pub state: IssueState,
    /// Labels
    pub labels: Vec<IssueLabel>,
    /// Assignees (usernames)
    pub assignees: Vec<String>,
    /// Author username
    pub author: String,
    /// Created timestamp (ISO 8601)
    pub created_at: String,
    /// Updated timestamp (ISO 8601)
    pub updated_at: String,
}

/// Options for creating an issue
#[derive(Debug, Clone, Default)]
pub struct IssueCreateOptions {
    /// Issue title
    pub title: String,
    /// Issue body/description
    pub body: Option<String>,
    /// Labels to apply
    pub labels: Vec<String>,
    /// Assignees (usernames)
    pub assignees: Vec<String>,
}

/// Result of creating an issue
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IssueCreateResult {
    /// Issue number
    pub number: u64,
    /// Issue URL
    pub url: String,
}

/// Filter options for listing issues
#[derive(Debug, Clone, Default)]
pub struct IssueListFilter {
    /// Filter by state (default: open)
    pub state: Option<IssueState>,
    /// Filter by labels
    pub labels: Vec<String>,
    /// Filter by assignee
    pub assignee: Option<String>,
    /// Maximum results to return
    pub limit: Option<u32>,
}

/// Azure DevOps specific context
#[derive(Debug, Clone)]
pub struct AzureDevOpsContext {
    /// Organization name
    pub organization: String,
    /// Project name
    pub project: String,
    /// Repository name
    pub repository: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── Display traits ──────────────────────────────────────────

    #[test]
    fn test_pr_state_display() {
        assert_eq!(PRState::Open.to_string(), "open");
        assert_eq!(PRState::Closed.to_string(), "closed");
        assert_eq!(PRState::Merged.to_string(), "merged");
    }

    #[test]
    fn test_check_state_display() {
        assert_eq!(CheckState::Pending.to_string(), "pending");
        assert_eq!(CheckState::Success.to_string(), "success");
        assert_eq!(CheckState::Failure.to_string(), "failure");
    }

    #[test]
    fn test_merge_method_display() {
        assert_eq!(MergeMethod::Merge.to_string(), "merge");
        assert_eq!(MergeMethod::Squash.to_string(), "squash");
        assert_eq!(MergeMethod::Rebase.to_string(), "rebase");
    }

    // ── Default implementations ─────────────────────────────────

    #[test]
    fn test_pr_state_default() {
        assert_eq!(PRState::default(), PRState::Open);
    }

    #[test]
    fn test_check_state_default() {
        assert_eq!(CheckState::default(), CheckState::Pending);
    }

    #[test]
    fn test_merge_method_default() {
        assert_eq!(MergeMethod::default(), MergeMethod::Merge);
    }

    #[test]
    fn test_merge_method_value_enum() {
        use clap::ValueEnum;

        // Valid values parse correctly
        let variants = MergeMethod::value_variants();
        assert_eq!(variants.len(), 3);

        // to_possible_value returns lowercase strings matching the API
        assert_eq!(
            MergeMethod::Merge.to_possible_value().unwrap().get_name(),
            "merge"
        );
        assert_eq!(
            MergeMethod::Squash.to_possible_value().unwrap().get_name(),
            "squash"
        );
        assert_eq!(
            MergeMethod::Rebase.to_possible_value().unwrap().get_name(),
            "rebase"
        );
    }

    #[test]
    fn test_allowed_merge_methods_default() {
        let methods = AllowedMergeMethods::default();
        assert!(methods.merge);
        assert!(methods.squash);
        assert!(methods.rebase);
    }

    #[test]
    fn test_pr_create_options_default() {
        let opts = PRCreateOptions::default();
        assert!(opts.title.is_empty());
        assert!(opts.body.is_none());
        assert!(opts.base.is_none());
        assert!(opts.draft.is_none());
    }

    #[test]
    fn test_pr_merge_options_default() {
        let opts = PRMergeOptions::default();
        assert!(opts.method.is_none());
        assert!(opts.delete_branch.is_none());
    }

    // ── Serde serialization ─────────────────────────────────────

    #[test]
    fn test_pr_state_serde_roundtrip() {
        let json = serde_json::to_string(&PRState::Open).unwrap();
        assert_eq!(json, "\"open\"");

        let json = serde_json::to_string(&PRState::Closed).unwrap();
        assert_eq!(json, "\"closed\"");

        let json = serde_json::to_string(&PRState::Merged).unwrap();
        assert_eq!(json, "\"merged\"");

        // Deserialize
        let state: PRState = serde_json::from_str("\"open\"").unwrap();
        assert_eq!(state, PRState::Open);

        let state: PRState = serde_json::from_str("\"merged\"").unwrap();
        assert_eq!(state, PRState::Merged);
    }

    #[test]
    fn test_check_state_serde_roundtrip() {
        let json = serde_json::to_string(&CheckState::Success).unwrap();
        assert_eq!(json, "\"success\"");

        let state: CheckState = serde_json::from_str("\"failure\"").unwrap();
        assert_eq!(state, CheckState::Failure);

        let state: CheckState = serde_json::from_str("\"pending\"").unwrap();
        assert_eq!(state, CheckState::Pending);
    }

    #[test]
    fn test_pull_request_serde_roundtrip() {
        let pr = PullRequest {
            number: 42,
            url: "https://github.com/owner/repo/pull/42".to_string(),
            title: "Test PR".to_string(),
            body: "Description".to_string(),
            state: PRState::Open,
            merged: false,
            mergeable: Some(true),
            head: PRHead {
                ref_name: "feat/test".to_string(),
                sha: "abc123".to_string(),
            },
            base: PRBase {
                ref_name: "main".to_string(),
            },
        };

        let json = serde_json::to_string(&pr).unwrap();
        let deserialized: PullRequest = serde_json::from_str(&json).unwrap();

        assert_eq!(deserialized.number, 42);
        assert_eq!(deserialized.title, "Test PR");
        assert_eq!(deserialized.state, PRState::Open);
        assert!(!deserialized.merged);
        assert_eq!(deserialized.mergeable, Some(true));
        assert_eq!(deserialized.head.ref_name, "feat/test");
        assert_eq!(deserialized.head.sha, "abc123");
        assert_eq!(deserialized.base.ref_name, "main");
    }

    #[test]
    fn test_pr_head_serde_ref_rename() {
        // The "ref" field is renamed from "ref_name" via serde
        let head = PRHead {
            ref_name: "feat/branch".to_string(),
            sha: "def456".to_string(),
        };
        let json = serde_json::to_string(&head).unwrap();
        assert!(json.contains("\"ref\""));
        assert!(!json.contains("\"ref_name\""));

        let parsed: PRHead = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.ref_name, "feat/branch");
    }

    #[test]
    fn test_pr_create_result_serde() {
        let result = PRCreateResult {
            number: 99,
            url: "https://github.com/owner/repo/pull/99".to_string(),
        };
        let json = serde_json::to_string(&result).unwrap();
        let deserialized: PRCreateResult = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.number, 99);
        assert_eq!(deserialized.url, "https://github.com/owner/repo/pull/99");
    }

    #[test]
    fn test_pr_review_serde() {
        let review = PRReview {
            state: "APPROVED".to_string(),
            user: "reviewer".to_string(),
        };
        let json = serde_json::to_string(&review).unwrap();
        let deserialized: PRReview = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.state, "APPROVED");
        assert_eq!(deserialized.user, "reviewer");
    }

    #[test]
    fn test_status_check_result_serde() {
        let result = StatusCheckResult {
            state: CheckState::Success,
            statuses: vec![
                StatusCheck {
                    context: "CI".to_string(),
                    state: "success".to_string(),
                },
                StatusCheck {
                    context: "Tests".to_string(),
                    state: "success".to_string(),
                },
            ],
        };

        let json = serde_json::to_string(&result).unwrap();
        let deserialized: StatusCheckResult = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.state, CheckState::Success);
        assert_eq!(deserialized.statuses.len(), 2);
        assert_eq!(deserialized.statuses[0].context, "CI");
    }

    #[test]
    fn test_check_status_details_serde() {
        let details = CheckStatusDetails {
            state: CheckState::Failure,
            passed: 3,
            failed: 1,
            pending: 0,
            skipped: 2,
            total: 6,
        };

        let json = serde_json::to_string(&details).unwrap();
        let deserialized: CheckStatusDetails = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.state, CheckState::Failure);
        assert_eq!(deserialized.passed, 3);
        assert_eq!(deserialized.failed, 1);
        assert_eq!(deserialized.total, 6);
    }

    #[test]
    fn test_allowed_merge_methods_serde() {
        let methods = AllowedMergeMethods {
            merge: true,
            squash: false,
            rebase: true,
        };

        let json = serde_json::to_string(&methods).unwrap();
        let deserialized: AllowedMergeMethods = serde_json::from_str(&json).unwrap();
        assert!(deserialized.merge);
        assert!(!deserialized.squash);
        assert!(deserialized.rebase);
    }

    // ── Equality ────────────────────────────────────────────────

    #[test]
    fn test_pr_state_equality() {
        assert_eq!(PRState::Open, PRState::Open);
        assert_ne!(PRState::Open, PRState::Closed);
        assert_ne!(PRState::Closed, PRState::Merged);
    }

    #[test]
    fn test_check_state_equality() {
        assert_eq!(CheckState::Success, CheckState::Success);
        assert_ne!(CheckState::Success, CheckState::Failure);
        assert_ne!(CheckState::Pending, CheckState::Success);
    }

    #[test]
    fn test_merge_method_equality() {
        assert_eq!(MergeMethod::Squash, MergeMethod::Squash);
        assert_ne!(MergeMethod::Merge, MergeMethod::Rebase);
    }

    // ── Issue types ─────────────────────────────────────────────

    #[test]
    fn test_issue_state_display() {
        assert_eq!(IssueState::Open.to_string(), "open");
        assert_eq!(IssueState::Closed.to_string(), "closed");
    }

    #[test]
    fn test_issue_state_default() {
        assert_eq!(IssueState::default(), IssueState::Open);
    }

    #[test]
    fn test_issue_state_equality() {
        assert_eq!(IssueState::Open, IssueState::Open);
        assert_ne!(IssueState::Open, IssueState::Closed);
    }

    #[test]
    fn test_issue_state_serde_roundtrip() {
        let json = serde_json::to_string(&IssueState::Open).unwrap();
        assert_eq!(json, "\"open\"");

        let json = serde_json::to_string(&IssueState::Closed).unwrap();
        assert_eq!(json, "\"closed\"");

        let state: IssueState = serde_json::from_str("\"open\"").unwrap();
        assert_eq!(state, IssueState::Open);

        let state: IssueState = serde_json::from_str("\"closed\"").unwrap();
        assert_eq!(state, IssueState::Closed);
    }

    #[test]
    fn test_issue_serde_roundtrip() {
        let issue = Issue {
            number: 42,
            url: "https://github.com/owner/repo/issues/42".to_string(),
            title: "Bug report".to_string(),
            body: "Description of bug".to_string(),
            state: IssueState::Open,
            labels: vec![IssueLabel {
                name: "bug".to_string(),
                color: Some("d73a4a".to_string()),
            }],
            assignees: vec!["user1".to_string()],
            author: "reporter".to_string(),
            created_at: "2024-01-01T00:00:00Z".to_string(),
            updated_at: "2024-01-02T00:00:00Z".to_string(),
        };

        let json = serde_json::to_string(&issue).unwrap();
        let deserialized: Issue = serde_json::from_str(&json).unwrap();

        assert_eq!(deserialized.number, 42);
        assert_eq!(deserialized.title, "Bug report");
        assert_eq!(deserialized.state, IssueState::Open);
        assert_eq!(deserialized.labels.len(), 1);
        assert_eq!(deserialized.labels[0].name, "bug");
        assert_eq!(deserialized.assignees, vec!["user1"]);
        assert_eq!(deserialized.author, "reporter");
    }

    #[test]
    fn test_issue_create_result_serde() {
        let result = IssueCreateResult {
            number: 99,
            url: "https://github.com/owner/repo/issues/99".to_string(),
        };
        let json = serde_json::to_string(&result).unwrap();
        let deserialized: IssueCreateResult = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.number, 99);
        assert_eq!(deserialized.url, "https://github.com/owner/repo/issues/99");
    }

    #[test]
    fn test_issue_create_options_default() {
        let opts = IssueCreateOptions::default();
        assert!(opts.title.is_empty());
        assert!(opts.body.is_none());
        assert!(opts.labels.is_empty());
        assert!(opts.assignees.is_empty());
    }

    #[test]
    fn test_issue_list_filter_default() {
        let filter = IssueListFilter::default();
        assert!(filter.state.is_none());
        assert!(filter.labels.is_empty());
        assert!(filter.assignee.is_none());
        assert!(filter.limit.is_none());
    }

    #[test]
    fn test_issue_label_serde() {
        let label = IssueLabel {
            name: "enhancement".to_string(),
            color: Some("a2eeef".to_string()),
        };
        let json = serde_json::to_string(&label).unwrap();
        let deserialized: IssueLabel = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.name, "enhancement");
        assert_eq!(deserialized.color, Some("a2eeef".to_string()));
    }
}

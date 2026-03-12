//! Hosting platform trait definition

use async_trait::async_trait;
use thiserror::Error;

use super::types::*;
use crate::core::manifest::PlatformType;

/// Errors that can occur during platform operations
#[derive(Error, Debug)]
pub enum PlatformError {
    #[error("Authentication failed: {0}")]
    AuthError(String),

    #[error("API error: {0}")]
    ApiError(String),

    #[error("Not found: {0}")]
    NotFound(String),

    #[error("Rate limited")]
    RateLimited,

    #[error("Network error: {0}")]
    NetworkError(String),

    #[error("Parse error: {0}")]
    ParseError(String),

    #[error("Branch is behind base: {0}")]
    BranchBehind(String),

    #[error("Branch protection prevents merge: {0}")]
    BranchProtected(String),
}

/// Linked PR reference for cross-repo tracking
#[derive(Debug, Clone)]
pub struct LinkedPRRef {
    pub repo_name: String,
    pub number: u64,
}

/// Interface for hosting platform adapters
/// Each platform (GitHub, GitLab, Azure DevOps) implements this trait
#[async_trait]
pub trait HostingPlatform: Send + Sync {
    /// Platform type identifier
    fn platform_type(&self) -> PlatformType;

    /// Get authentication token for API calls
    async fn get_token(&self) -> Result<String, PlatformError>;

    /// Create a pull request
    async fn create_pull_request(
        &self,
        owner: &str,
        repo: &str,
        head: &str,
        base: &str,
        title: &str,
        body: Option<&str>,
        draft: bool,
    ) -> Result<PRCreateResult, PlatformError>;

    /// Get pull request details
    async fn get_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<PullRequest, PlatformError>;

    /// Update pull request body
    async fn update_pull_request_body(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        body: &str,
    ) -> Result<(), PlatformError>;

    /// Update pull request title and/or body
    ///
    /// At least one of `title` or `body` must be `Some`.
    async fn update_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        title: Option<&str>,
        body: Option<&str>,
    ) -> Result<(), PlatformError> {
        // Default: delegate to update_pull_request_body if only body is provided.
        // Platforms that support title updates should override this.
        // Check title first to avoid partial updates (body mutated, then title errors).
        if title.is_some() {
            return Err(PlatformError::ApiError(
                "Updating PR title is not supported on this platform".to_string(),
            ));
        }
        if let Some(body_text) = body {
            self.update_pull_request_body(owner, repo, pull_number, body_text)
                .await?;
        }
        Ok(())
    }

    /// Merge a pull request
    async fn merge_pull_request(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
        method: Option<MergeMethod>,
        delete_branch: bool,
    ) -> Result<bool, PlatformError>;

    /// Find an open PR by branch name
    async fn find_pr_by_branch(
        &self,
        owner: &str,
        repo: &str,
        branch: &str,
    ) -> Result<Option<PRCreateResult>, PlatformError>;

    /// Check if PR is approved
    async fn is_pull_request_approved(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<bool, PlatformError>;

    /// Get reviews for a PR
    async fn get_pull_request_reviews(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<Vec<PRReview>, PlatformError>;

    /// Get CI/CD status checks for a commit
    async fn get_status_checks(
        &self,
        owner: &str,
        repo: &str,
        ref_name: &str,
    ) -> Result<StatusCheckResult, PlatformError>;

    /// Get allowed merge methods for a repository
    async fn get_allowed_merge_methods(
        &self,
        owner: &str,
        repo: &str,
    ) -> Result<AllowedMergeMethods, PlatformError>;

    /// Get the diff for a pull request
    async fn get_pull_request_diff(
        &self,
        owner: &str,
        repo: &str,
        pull_number: u64,
    ) -> Result<String, PlatformError>;

    /// Parse a git URL to extract owner/repo information
    fn parse_repo_url(&self, url: &str) -> Option<ParsedRepoInfo>;

    /// Check if a URL belongs to this platform
    fn matches_url(&self, url: &str) -> bool;

    /// Create a new repository on the platform
    ///
    /// This is an optional operation - platforms may not support it or the user
    /// may not have permission. Returns the clone URL of the created repository.
    async fn create_repository(
        &self,
        owner: &str,
        name: &str,
        description: Option<&str>,
        private: bool,
    ) -> Result<String, PlatformError> {
        // Default implementation returns an error
        let _ = (owner, name, description, private);
        Err(PlatformError::ApiError(
            "Repository creation not supported on this platform".to_string(),
        ))
    }

    /// Delete a repository from the platform
    ///
    /// This is a destructive operation and should be used with caution.
    /// Mainly useful for testing cleanup.
    async fn delete_repository(&self, owner: &str, name: &str) -> Result<(), PlatformError> {
        // Default implementation returns an error
        let _ = (owner, name);
        Err(PlatformError::ApiError(
            "Repository deletion not supported on this platform".to_string(),
        ))
    }

    /// Update a pull request branch (merge base into head)
    ///
    /// Returns Ok(true) if the branch was updated, Ok(false) if already up to date.
    /// Returns Err if the update failed (e.g., conflicts).
    async fn update_branch(
        &self,
        _owner: &str,
        _repo: &str,
        _pull_number: u64,
    ) -> Result<bool, PlatformError> {
        Err(PlatformError::ApiError(
            "Branch update not supported on this platform".to_string(),
        ))
    }

    /// Enable auto-merge for a pull request
    ///
    /// The PR will be automatically merged when all required checks pass.
    /// Returns Ok(true) if auto-merge was enabled.
    async fn enable_auto_merge(
        &self,
        _owner: &str,
        _repo: &str,
        _pull_number: u64,
        _method: Option<MergeMethod>,
    ) -> Result<bool, PlatformError> {
        Err(PlatformError::ApiError(
            "Auto-merge not supported on this platform".to_string(),
        ))
    }

    /// Create a release on the platform with a tag
    ///
    /// Creates a git tag and a release (e.g., GitHub Release) on the repository.
    /// Returns the release URL and metadata.
    async fn create_release(
        &self,
        _owner: &str,
        _repo: &str,
        _tag: &str,
        _name: &str,
        _body: Option<&str>,
        _target_commitish: &str,
        _draft: bool,
        _prerelease: bool,
    ) -> Result<ReleaseResult, PlatformError> {
        Err(PlatformError::ApiError(
            "Release creation not supported on this platform".to_string(),
        ))
    }

    /// List issues for a repository
    async fn list_issues(
        &self,
        _owner: &str,
        _repo: &str,
        _filter: &IssueListFilter,
    ) -> Result<Vec<Issue>, PlatformError> {
        Err(PlatformError::ApiError(
            "Issue listing not supported on this platform".to_string(),
        ))
    }

    /// Create an issue on a repository
    async fn create_issue(
        &self,
        _owner: &str,
        _repo: &str,
        _options: &IssueCreateOptions,
    ) -> Result<IssueCreateResult, PlatformError> {
        Err(PlatformError::ApiError(
            "Issue creation not supported on this platform".to_string(),
        ))
    }

    /// Get a single issue by number
    async fn get_issue(
        &self,
        _owner: &str,
        _repo: &str,
        _issue_number: u64,
    ) -> Result<Issue, PlatformError> {
        Err(PlatformError::ApiError(
            "Issue retrieval not supported on this platform".to_string(),
        ))
    }

    /// Close an issue
    async fn close_issue(
        &self,
        _owner: &str,
        _repo: &str,
        _issue_number: u64,
    ) -> Result<(), PlatformError> {
        Err(PlatformError::ApiError(
            "Issue closing not supported on this platform".to_string(),
        ))
    }

    /// Reopen an issue
    async fn reopen_issue(
        &self,
        _owner: &str,
        _repo: &str,
        _issue_number: u64,
    ) -> Result<(), PlatformError> {
        Err(PlatformError::ApiError(
            "Issue reopening not supported on this platform".to_string(),
        ))
    }

    /// Generate HTML comment for linked PR tracking
    fn generate_linked_pr_comment(&self, links: &[LinkedPRRef]) -> String {
        if links.is_empty() {
            return String::new();
        }

        let mut comment = String::from("<!-- gitgrip-linked-prs\n");
        for link in links {
            comment.push_str(&format!("{}:{}\n", link.repo_name, link.number));
        }
        comment.push_str("-->");
        comment
    }

    /// Parse linked PR references from PR body
    fn parse_linked_pr_comment(&self, body: &str) -> Vec<LinkedPRRef> {
        let start_marker = "<!-- gitgrip-linked-prs";
        let end_marker = "-->";

        let Some(start) = body.find(start_marker) else {
            return Vec::new();
        };

        let content_start = start + start_marker.len();
        let Some(end) = body[content_start..].find(end_marker) else {
            return Vec::new();
        };

        let content = &body[content_start..content_start + end];

        content
            .lines()
            .filter_map(|line| {
                let line = line.trim();
                if line.is_empty() {
                    return None;
                }

                let parts: Vec<&str> = line.splitn(2, ':').collect();
                if parts.len() != 2 {
                    return None;
                }

                let number = parts[1].parse().ok()?;
                Some(LinkedPRRef {
                    repo_name: parts[0].to_string(),
                    number,
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MockPlatform;

    #[async_trait]
    impl HostingPlatform for MockPlatform {
        fn platform_type(&self) -> PlatformType {
            PlatformType::GitHub
        }

        async fn get_token(&self) -> Result<String, PlatformError> {
            Ok("mock-token".to_string())
        }

        async fn create_pull_request(
            &self,
            _owner: &str,
            _repo: &str,
            _head: &str,
            _base: &str,
            _title: &str,
            _body: Option<&str>,
            _draft: bool,
        ) -> Result<PRCreateResult, PlatformError> {
            unimplemented!()
        }

        async fn get_pull_request(
            &self,
            _owner: &str,
            _repo: &str,
            _pull_number: u64,
        ) -> Result<PullRequest, PlatformError> {
            unimplemented!()
        }

        async fn update_pull_request_body(
            &self,
            _owner: &str,
            _repo: &str,
            _pull_number: u64,
            _body: &str,
        ) -> Result<(), PlatformError> {
            unimplemented!()
        }

        async fn merge_pull_request(
            &self,
            _owner: &str,
            _repo: &str,
            _pull_number: u64,
            _method: Option<MergeMethod>,
            _delete_branch: bool,
        ) -> Result<bool, PlatformError> {
            unimplemented!()
        }

        async fn find_pr_by_branch(
            &self,
            _owner: &str,
            _repo: &str,
            _branch: &str,
        ) -> Result<Option<PRCreateResult>, PlatformError> {
            unimplemented!()
        }

        async fn is_pull_request_approved(
            &self,
            _owner: &str,
            _repo: &str,
            _pull_number: u64,
        ) -> Result<bool, PlatformError> {
            unimplemented!()
        }

        async fn get_pull_request_reviews(
            &self,
            _owner: &str,
            _repo: &str,
            _pull_number: u64,
        ) -> Result<Vec<PRReview>, PlatformError> {
            unimplemented!()
        }

        async fn get_status_checks(
            &self,
            _owner: &str,
            _repo: &str,
            _ref_name: &str,
        ) -> Result<StatusCheckResult, PlatformError> {
            unimplemented!()
        }

        async fn get_allowed_merge_methods(
            &self,
            _owner: &str,
            _repo: &str,
        ) -> Result<AllowedMergeMethods, PlatformError> {
            unimplemented!()
        }

        async fn get_pull_request_diff(
            &self,
            _owner: &str,
            _repo: &str,
            _pull_number: u64,
        ) -> Result<String, PlatformError> {
            unimplemented!()
        }

        fn parse_repo_url(&self, _url: &str) -> Option<ParsedRepoInfo> {
            None
        }

        fn matches_url(&self, _url: &str) -> bool {
            false
        }
    }

    #[test]
    fn test_generate_linked_pr_comment() {
        let platform = MockPlatform;
        let links = vec![
            LinkedPRRef {
                repo_name: "app".to_string(),
                number: 123,
            },
            LinkedPRRef {
                repo_name: "lib".to_string(),
                number: 456,
            },
        ];

        let comment = platform.generate_linked_pr_comment(&links);
        assert!(comment.contains("app:123"));
        assert!(comment.contains("lib:456"));
    }

    #[test]
    fn test_parse_linked_pr_comment() {
        let platform = MockPlatform;
        let body = r#"
Some PR description

<!-- gitgrip-linked-prs
app:123
lib:456
-->

More content
"#;

        let links = platform.parse_linked_pr_comment(body);
        assert_eq!(links.len(), 2);
        assert_eq!(links[0].repo_name, "app");
        assert_eq!(links[0].number, 123);
        assert_eq!(links[1].repo_name, "lib");
        assert_eq!(links[1].number, 456);
    }

    #[test]
    fn test_parse_empty_comment() {
        let platform = MockPlatform;
        let links = platform.parse_linked_pr_comment("No linked PRs here");
        assert!(links.is_empty());
    }

    #[test]
    fn test_generate_linked_pr_comment_empty() {
        let platform = MockPlatform;
        assert_eq!(platform.generate_linked_pr_comment(&[]), "");
    }

    #[test]
    fn test_parse_linked_pr_comment_unterminated() {
        let platform = MockPlatform;
        let body = "<!-- gitgrip-linked-prs\napp:42\n";
        assert!(platform.parse_linked_pr_comment(body).is_empty());
    }

    #[test]
    fn test_parse_linked_pr_comment_malformed_lines() {
        let platform = MockPlatform;
        let body = "<!-- gitgrip-linked-prs\nno-colon-here\napp:notanumber\nvalid:99\n-->";
        let links = platform.parse_linked_pr_comment(body);
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].repo_name, "valid");
        assert_eq!(links[0].number, 99);
    }

    #[test]
    fn test_parse_linked_pr_comment_with_surrounding_text() {
        let platform = MockPlatform;
        let body = "PR description here.\n\n<!-- gitgrip-linked-prs\nrepo:1\n-->\n\nMore text.";
        let links = platform.parse_linked_pr_comment(body);
        assert_eq!(links.len(), 1);
        assert_eq!(links[0].repo_name, "repo");
    }

    #[test]
    fn test_generate_linked_pr_comment_format() {
        let platform = MockPlatform;
        let links = vec![LinkedPRRef {
            repo_name: "myapp".to_string(),
            number: 7,
        }];
        let comment = platform.generate_linked_pr_comment(&links);
        assert_eq!(comment, "<!-- gitgrip-linked-prs\nmyapp:7\n-->");
    }
}

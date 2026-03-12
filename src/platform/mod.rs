//! Hosting platform adapters
//!
//! Provides a unified interface for GitHub, GitLab, and Azure DevOps.

pub mod azure;
pub mod bitbucket;
pub mod capabilities;
pub mod github;
pub mod gitlab;
pub mod http;
pub mod rate_limit;
pub mod traits;
pub mod types;

pub use http::create_http_client;
pub use traits::HostingPlatform;
pub use types::{
    AllowedMergeMethods, CheckState, CheckStatusDetails, Issue, IssueCreateOptions,
    IssueCreateResult, IssueLabel, IssueListFilter, IssueState, MergeMethod, PRBase,
    PRCreateResult, PRHead, PRReview, PRState, ParsedRepoInfo, PullRequest, ReleaseResult,
    StatusCheck, StatusCheckResult,
};

use crate::core::manifest::PlatformType;
use std::sync::Arc;

/// Get a platform adapter for the given platform type
pub fn get_platform_adapter(
    platform_type: PlatformType,
    base_url: Option<&str>,
) -> Arc<dyn HostingPlatform> {
    match platform_type {
        PlatformType::GitHub => Arc::new(github::GitHubAdapter::new(base_url)),
        PlatformType::GitLab => Arc::new(gitlab::GitLabAdapter::new(base_url)),
        PlatformType::AzureDevOps => Arc::new(azure::AzureDevOpsAdapter::new(base_url)),
        PlatformType::Bitbucket => Arc::new(bitbucket::BitbucketAdapter::new(base_url)),
    }
}

/// Detect platform type from a git URL
pub fn detect_platform(url: &str) -> PlatformType {
    // Check GitHub first (most common)
    if url.contains("github.com") {
        return PlatformType::GitHub;
    }

    // Check Azure DevOps before GitLab (avoid false positives)
    if url.contains("dev.azure.com") || url.contains("visualstudio.com") {
        return PlatformType::AzureDevOps;
    }

    // Check Bitbucket before GitLab
    if url.contains("bitbucket.org") || url.contains("bitbucket.") {
        return PlatformType::Bitbucket;
    }

    // Check GitLab - ensure it's in hostname, not just path
    if url.contains("gitlab.com") || url.contains("gitlab.") {
        return PlatformType::GitLab;
    }

    // Default to GitHub for backward compatibility
    PlatformType::GitHub
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_github() {
        assert_eq!(
            detect_platform("git@github.com:user/repo.git"),
            PlatformType::GitHub
        );
        assert_eq!(
            detect_platform("https://github.com/user/repo.git"),
            PlatformType::GitHub
        );
    }

    #[test]
    fn test_detect_gitlab() {
        assert_eq!(
            detect_platform("git@gitlab.com:user/repo.git"),
            PlatformType::GitLab
        );
    }

    #[test]
    fn test_detect_azure() {
        assert_eq!(
            detect_platform("https://dev.azure.com/org/project/_git/repo"),
            PlatformType::AzureDevOps
        );
        assert_eq!(
            detect_platform("git@ssh.dev.azure.com:v3/org/project/repo"),
            PlatformType::AzureDevOps
        );
    }

    #[test]
    fn test_default_to_github() {
        assert_eq!(
            detect_platform("git@unknown.com:user/repo.git"),
            PlatformType::GitHub
        );
    }
}

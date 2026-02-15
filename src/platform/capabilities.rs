//! Platform capability declarations
//!
//! Documents which optional operations each hosting platform supports,
//! allowing callers to check capability before attempting an operation.

use crate::core::manifest::PlatformType;

/// Optional capabilities that a hosting platform may support
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PlatformCapability {
    /// Create a pull/merge request
    CreatePr,
    /// Merge a pull/merge request
    MergePr,
    /// Find an open PR by branch name
    FindPrByBranch,
    /// Get PR review status
    GetReviews,
    /// Get CI/CD status checks
    StatusChecks,
    /// Get allowed merge methods
    MergeMethods,
    /// Get PR diff
    PrDiff,
    /// Update a PR branch (merge base into head)
    UpdateBranch,
    /// Enable auto-merge when checks pass
    AutoMerge,
    /// Create a repository on the platform
    CreateRepo,
    /// Delete a repository from the platform
    DeleteRepo,
    /// Create a tagged release
    CreateRelease,
}

/// Returns the capabilities supported by the given platform type.
///
/// Core PR operations (create, merge, find, reviews, checks, diff, merge methods)
/// are implemented by all platforms. Optional operations vary.
pub fn platform_capabilities(platform: PlatformType) -> Vec<PlatformCapability> {
    use PlatformCapability::*;

    // All platforms support core PR operations
    let mut caps = vec![
        CreatePr,
        MergePr,
        FindPrByBranch,
        GetReviews,
        StatusChecks,
        MergeMethods,
        PrDiff,
    ];

    match platform {
        PlatformType::GitHub => {
            caps.extend([
                UpdateBranch,
                AutoMerge,
                CreateRepo,
                DeleteRepo,
                CreateRelease,
            ]);
        }
        PlatformType::GitLab => {
            caps.extend([CreateRepo, DeleteRepo]);
        }
        PlatformType::AzureDevOps => {
            caps.extend([CreateRepo, DeleteRepo]);
        }
        PlatformType::Bitbucket => {
            // Bitbucket only supports core PR operations
        }
    }

    caps
}

/// Check if a platform supports a specific capability
pub fn platform_supports(platform: PlatformType, capability: PlatformCapability) -> bool {
    platform_capabilities(platform).contains(&capability)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_github_has_all_capabilities() {
        let caps = platform_capabilities(PlatformType::GitHub);
        assert!(caps.contains(&PlatformCapability::CreatePr));
        assert!(caps.contains(&PlatformCapability::AutoMerge));
        assert!(caps.contains(&PlatformCapability::UpdateBranch));
        assert!(caps.contains(&PlatformCapability::CreateRepo));
        assert!(caps.contains(&PlatformCapability::DeleteRepo));
        assert!(caps.contains(&PlatformCapability::CreateRelease));
    }

    #[test]
    fn test_gitlab_has_repo_management() {
        let caps = platform_capabilities(PlatformType::GitLab);
        assert!(caps.contains(&PlatformCapability::CreatePr));
        assert!(caps.contains(&PlatformCapability::CreateRepo));
        assert!(caps.contains(&PlatformCapability::DeleteRepo));
        assert!(!caps.contains(&PlatformCapability::AutoMerge));
        assert!(!caps.contains(&PlatformCapability::CreateRelease));
    }

    #[test]
    fn test_azure_has_repo_management() {
        let caps = platform_capabilities(PlatformType::AzureDevOps);
        assert!(caps.contains(&PlatformCapability::CreatePr));
        assert!(caps.contains(&PlatformCapability::CreateRepo));
        assert!(!caps.contains(&PlatformCapability::AutoMerge));
    }

    #[test]
    fn test_bitbucket_core_only() {
        let caps = platform_capabilities(PlatformType::Bitbucket);
        assert!(caps.contains(&PlatformCapability::CreatePr));
        assert!(caps.contains(&PlatformCapability::MergePr));
        assert!(!caps.contains(&PlatformCapability::CreateRepo));
        assert!(!caps.contains(&PlatformCapability::AutoMerge));
        assert!(!caps.contains(&PlatformCapability::CreateRelease));
    }

    #[test]
    fn test_platform_supports() {
        assert!(platform_supports(
            PlatformType::GitHub,
            PlatformCapability::AutoMerge
        ));
        assert!(!platform_supports(
            PlatformType::Bitbucket,
            PlatformCapability::AutoMerge
        ));
    }
}

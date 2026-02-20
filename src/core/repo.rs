//! Repository information and operations

use std::path::{Path, PathBuf};

use crate::core::manifest::{
    Manifest, ManifestRepoConfig, ManifestSettings, PlatformType, RemoteConfig, RepoAgentConfig,
    RepoConfig,
};
use crate::core::manifest_paths;
use std::collections::HashMap;

/// Extended repository information with computed fields
#[derive(Debug, Clone)]
pub struct RepoInfo {
    /// Repository name (from manifest key)
    pub name: String,
    /// Git URL (resolved — explicit or derived from remote)
    pub url: String,
    /// Local path relative to manifest root
    pub path: String,
    /// Absolute path on disk
    pub absolute_path: PathBuf,
    /// Upstream revision (resolved: repo → settings → "main")
    pub revision: String,
    /// Workflow target branch name (resolved: repo → settings → revision)
    pub target: String,
    /// Remote for fetch/rebase (resolved: repo → settings → "origin")
    pub sync_remote: String,
    /// Remote for push (resolved: repo → settings → "origin")
    pub push_remote: String,
    /// Owner/namespace from git URL
    pub owner: String,
    /// Repo name from git URL
    pub repo: String,
    /// Detected or configured platform type
    pub platform_type: PlatformType,
    /// Optional base URL for self-hosted platform instances
    pub platform_base_url: Option<String>,
    /// Project name (Azure DevOps only)
    pub project: Option<String>,
    /// Reference repo (read-only, excluded from branch/PR operations)
    pub reference: bool,
    /// Groups this repo belongs to (for selective operations)
    pub groups: Vec<String>,
    /// Agent context metadata (build/test/lint commands for AI agents)
    pub agent: Option<RepoAgentConfig>,
}

impl RepoInfo {
    /// Create RepoInfo from a manifest RepoConfig
    pub fn from_config(
        name: &str,
        config: &RepoConfig,
        workspace_root: &PathBuf,
        settings: &ManifestSettings,
        remotes: Option<&HashMap<String, RemoteConfig>>,
    ) -> Option<Self> {
        // Resolve URL: explicit url, or derive from top-level remote
        let url = config
            .url
            .clone()
            .or_else(|| {
                config.remote.as_ref().and_then(|remote_name| {
                    remotes?.get(remote_name).map(|rc| {
                        let base = rc.fetch.trim_end_matches('/');
                        format!("{}/{}.git", base, name)
                    })
                })
            })
            .filter(|u| !u.is_empty())?;

        let parsed = parse_git_url(&url)?;

        let absolute_path = workspace_root.join(&config.path);

        let platform_type = config
            .platform
            .as_ref()
            .map(|p| p.platform_type)
            .unwrap_or_else(|| detect_platform(&url));
        let platform_base_url = config.platform.as_ref().and_then(|p| p.base_url.clone());

        // Resolve revision: repo → settings → "main"
        let revision = config
            .revision
            .clone()
            .or_else(|| settings.revision.clone())
            .unwrap_or_else(|| "main".to_string());

        // Resolve target: repo.target → settings.target → revision
        let target = config
            .target
            .clone()
            .or_else(|| settings.target.clone())
            .unwrap_or_else(|| revision.clone());

        // Resolve sync_remote: repo → settings → "origin"
        let sync_remote = config
            .sync_remote
            .clone()
            .or_else(|| settings.sync_remote.clone())
            .unwrap_or_else(|| "origin".to_string());

        // Resolve push_remote: repo → settings → "origin"
        let push_remote = config
            .push_remote
            .clone()
            .or_else(|| settings.push_remote.clone())
            .unwrap_or_else(|| "origin".to_string());

        Some(Self {
            name: name.to_string(),
            url,
            path: config.path.clone(),
            absolute_path,
            revision,
            target,
            sync_remote,
            push_remote,
            owner: parsed.owner,
            repo: parsed.repo,
            platform_type,
            platform_base_url,
            project: parsed.project,
            reference: config.reference,
            groups: config.groups.clone(),
            agent: config.agent.clone(),
        })
    }

    /// Get the workflow target branch name (for PR base, prune, etc.)
    pub fn target_branch(&self) -> &str {
        &self.target
    }

    /// Build the sync ref: "sync_remote/target" (e.g. "upstream/main")
    pub fn sync_ref(&self) -> String {
        format!("{}/{}", self.sync_remote, self.target)
    }

    /// Check if the repository exists on disk
    pub fn exists(&self) -> bool {
        self.absolute_path.join(".git").exists()
    }
}

/// Parsed git URL components
struct ParsedUrl {
    owner: String,
    repo: String,
    project: Option<String>,
}

/// Parse a git URL to extract owner and repo
fn parse_git_url(url: &str) -> Option<ParsedUrl> {
    // Handle SSH URLs: git@github.com:owner/repo.git
    if url.starts_with("git@") {
        let parts: Vec<&str> = url.splitn(2, ':').collect();
        if parts.len() != 2 {
            return None;
        }
        let path = parts[1].trim_end_matches(".git");

        // Handle Azure DevOps SSH: git@ssh.dev.azure.com:v3/org/project/repo
        if url.contains("dev.azure.com") || url.contains("visualstudio.com") {
            let segments: Vec<&str> = path.split('/').collect();
            if segments.len() >= 4 && segments[0] == "v3" {
                return Some(ParsedUrl {
                    owner: segments[1].to_string(),
                    repo: segments[3].to_string(),
                    project: Some(segments[2].to_string()),
                });
            }
        }

        // Standard format: owner/repo
        let segments: Vec<&str> = path.split('/').collect();
        if segments.len() >= 2 {
            return Some(ParsedUrl {
                owner: segments[0].to_string(),
                repo: segments[segments.len() - 1].to_string(),
                project: None,
            });
        }
    }

    // Handle HTTPS URLs: https://github.com/owner/repo.git
    if url.starts_with("https://") || url.starts_with("http://") {
        let url_without_proto = url
            .trim_start_matches("https://")
            .trim_start_matches("http://");
        let path = url_without_proto
            .split_once('/')?
            .1
            .trim_end_matches(".git");

        // Handle Azure DevOps HTTPS: https://dev.azure.com/org/project/_git/repo
        if url.contains("dev.azure.com") {
            let segments: Vec<&str> = path.split('/').collect();
            if segments.len() >= 4 && segments[2] == "_git" {
                return Some(ParsedUrl {
                    owner: segments[0].to_string(),
                    repo: segments[3].to_string(),
                    project: Some(segments[1].to_string()),
                });
            }
        }

        // Handle visualstudio.com: https://org.visualstudio.com/project/_git/repo
        if url.contains("visualstudio.com") {
            // Extract org from subdomain
            let host_and_path: Vec<&str> = url_without_proto.splitn(2, '/').collect();
            if host_and_path.len() < 2 {
                return None;
            }
            let host = host_and_path[0];
            let org = host.split('.').next()?;
            let segments: Vec<&str> = path.split('/').collect();
            if segments.len() >= 3 && segments[1] == "_git" {
                return Some(ParsedUrl {
                    owner: org.to_string(),
                    repo: segments[2].to_string(),
                    project: Some(segments[0].to_string()),
                });
            }
        }

        // Standard format: owner/repo
        let segments: Vec<&str> = path.split('/').collect();
        if segments.len() >= 2 {
            return Some(ParsedUrl {
                owner: segments[0].to_string(),
                repo: segments[segments.len() - 1].to_string(),
                project: None,
            });
        }
    }

    // Handle file:// URLs (used in testing with local bare repos)
    if url.starts_with("file://") {
        let path = url.trim_start_matches("file://").trim_end_matches(".git");
        // Extract the last path component as repo name
        if let Some(name) = path.rsplit('/').next() {
            return Some(ParsedUrl {
                owner: "local".to_string(),
                repo: name.to_string(),
                project: None,
            });
        }
    }

    None
}

/// Filter repos from a manifest by name, group, and reference status.
///
/// Replaces the repeated `.iter().filter_map().filter().filter().collect()` pattern
/// found across commands.
pub fn filter_repos(
    manifest: &Manifest,
    workspace_root: &PathBuf,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
    include_reference: bool,
) -> Vec<RepoInfo> {
    manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(
                name,
                config,
                workspace_root,
                &manifest.settings,
                manifest.remotes.as_ref(),
            )
        })
        .filter(|r| include_reference || !r.reference)
        .filter(|r| {
            repos_filter
                .map(|filter| filter.iter().any(|f| f == &r.name))
                .unwrap_or(true)
        })
        .filter(|r| {
            group_filter
                .map(|groups| r.groups.iter().any(|g| groups.contains(g)))
                .unwrap_or(true)
        })
        .collect()
}

/// Get RepoInfo for the manifest repo if it exists.
///
/// This provides a standardized way to include the manifest repository
/// in operations like sync, branch, checkout, push, and diff.
pub fn get_manifest_repo_info(manifest: &Manifest, workspace_root: &Path) -> Option<RepoInfo> {
    let manifest_config = manifest.manifest.as_ref()?;
    let manifests_dir = manifest_paths::resolve_manifest_repo_dir(workspace_root)?;

    // Only return if the manifest repo actually exists as a git repo
    if !manifests_dir.join(".git").exists() {
        return None;
    }

    create_manifest_repo_info(manifest_config, &manifest.settings, workspace_root)
}

/// Create RepoInfo from ManifestRepoConfig
fn create_manifest_repo_info(
    config: &ManifestRepoConfig,
    settings: &ManifestSettings,
    workspace_root: &Path,
) -> Option<RepoInfo> {
    let repo_dir = manifest_paths::resolve_manifest_repo_dir(workspace_root)
        .unwrap_or_else(|| manifest_paths::main_space_dir(workspace_root));
    let path = repo_dir
        .strip_prefix(workspace_root)
        .ok()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| manifest_paths::MAIN_SPACE_DIR.to_string());

    RepoInfo::from_config(
        "manifest",
        &RepoConfig {
            url: Some(config.url.clone()),
            remote: None,
            path,
            revision: config.revision.clone(),
            target: None,
            sync_remote: None,
            push_remote: None,
            copyfile: config.copyfile.clone(),
            linkfile: config.linkfile.clone(),
            platform: config.platform.clone(),
            reference: false,
            groups: Vec::new(),
            agent: None,
        },
        &workspace_root.to_path_buf(),
        settings,
        None,
    )
}

/// Detect platform type from URL
fn detect_platform(url: &str) -> PlatformType {
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
    fn test_parse_github_ssh() {
        let parsed = parse_git_url("git@github.com:user/repo.git").unwrap();
        assert_eq!(parsed.owner, "user");
        assert_eq!(parsed.repo, "repo");
        assert!(parsed.project.is_none());
    }

    #[test]
    fn test_parse_github_https() {
        let parsed = parse_git_url("https://github.com/user/repo.git").unwrap();
        assert_eq!(parsed.owner, "user");
        assert_eq!(parsed.repo, "repo");
    }

    #[test]
    fn test_parse_azure_https() {
        let parsed = parse_git_url("https://dev.azure.com/org/project/_git/repo").unwrap();
        assert_eq!(parsed.owner, "org");
        assert_eq!(parsed.repo, "repo");
        assert_eq!(parsed.project, Some("project".to_string()));
    }

    #[test]
    fn test_parse_azure_ssh() {
        let parsed = parse_git_url("git@ssh.dev.azure.com:v3/org/project/repo").unwrap();
        assert_eq!(parsed.owner, "org");
        assert_eq!(parsed.repo, "repo");
        assert_eq!(parsed.project, Some("project".to_string()));
    }

    #[test]
    fn test_parse_file_url() {
        let parsed = parse_git_url("file:///tmp/remotes/myrepo.git").unwrap();
        assert_eq!(parsed.owner, "local");
        assert_eq!(parsed.repo, "myrepo");
        assert!(parsed.project.is_none());
    }

    #[test]
    fn test_parse_file_url_no_extension() {
        let parsed = parse_git_url("file:///tmp/repos/test-repo").unwrap();
        assert_eq!(parsed.owner, "local");
        assert_eq!(parsed.repo, "test-repo");
    }

    #[test]
    fn test_detect_github() {
        assert_eq!(
            detect_platform("git@github.com:user/repo.git"),
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
    }

    #[test]
    fn test_get_manifest_repo_info_no_manifest() {
        use crate::core::manifest::Manifest;
        use std::collections::HashMap;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = get_manifest_repo_info(&manifest, temp.path());
        assert!(result.is_none());
    }

    #[test]
    fn test_get_manifest_repo_info_no_git_dir() {
        use crate::core::manifest::{Manifest, ManifestRepoConfig};
        use std::collections::HashMap;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: Some(ManifestRepoConfig {
                url: "git@github.com:user/manifest.git".to_string(),
                revision: Some("main".to_string()),
                copyfile: None,
                linkfile: None,
                composefile: None,
                platform: None,
            }),
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        // No manifest repo git directory exists
        let result = get_manifest_repo_info(&manifest, temp.path());
        assert!(result.is_none());
    }

    #[test]
    fn test_get_manifest_repo_info_with_git_dir() {
        use crate::core::manifest::{Manifest, ManifestRepoConfig};
        use std::collections::HashMap;
        use std::fs;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();

        // Create .gitgrip/spaces/main/.git directory
        let manifests_dir = temp.path().join(".gitgrip").join("spaces").join("main");
        fs::create_dir_all(manifests_dir.join(".git")).unwrap();

        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: Some(ManifestRepoConfig {
                url: "git@github.com:user/manifest.git".to_string(),
                revision: Some("main".to_string()),
                copyfile: None,
                linkfile: None,
                composefile: None,
                platform: None,
            }),
            repos: HashMap::new(),
            settings: Default::default(),
            workspace: None,
        };

        let result = get_manifest_repo_info(&manifest, temp.path());
        assert!(result.is_some());

        let info = result.unwrap();
        assert_eq!(info.name, "manifest");
        assert_eq!(info.path, ".gitgrip/spaces/main");
        assert_eq!(info.revision, "main");
        assert!(!info.reference);
    }

    #[test]
    fn test_detect_platform_bitbucket_org() {
        assert_eq!(
            detect_platform("git@bitbucket.org:team/repo.git"),
            PlatformType::Bitbucket
        );
    }

    #[test]
    fn test_detect_platform_self_hosted_bitbucket() {
        assert_eq!(
            detect_platform("https://bitbucket.example.com/team/repo.git"),
            PlatformType::Bitbucket
        );
    }

    #[test]
    fn test_detect_platform_self_hosted_gitlab() {
        assert_eq!(
            detect_platform("git@gitlab.company.com:team/repo.git"),
            PlatformType::GitLab
        );
    }

    #[test]
    fn test_detect_platform_visualstudio() {
        assert_eq!(
            detect_platform("https://org.visualstudio.com/project/_git/repo"),
            PlatformType::AzureDevOps
        );
    }

    #[test]
    fn test_detect_platform_unknown_defaults_to_github() {
        assert_eq!(
            detect_platform("https://custom-git.example.com/user/repo.git"),
            PlatformType::GitHub
        );
    }

    #[test]
    fn test_parse_visualstudio_url() {
        let parsed = parse_git_url("https://org.visualstudio.com/project/_git/repo").unwrap();
        assert_eq!(parsed.owner, "org");
        assert_eq!(parsed.repo, "repo");
        assert_eq!(parsed.project, Some("project".to_string()));
    }

    #[test]
    fn test_parse_http_url() {
        let parsed = parse_git_url("http://github.com/user/repo.git").unwrap();
        assert_eq!(parsed.owner, "user");
        assert_eq!(parsed.repo, "repo");
    }

    #[test]
    fn test_parse_invalid_url_returns_none() {
        assert!(parse_git_url("not-a-url").is_none());
        assert!(parse_git_url("").is_none());
    }

    #[test]
    fn test_filter_repos_by_name() {
        use crate::core::manifest::Manifest;
        use std::collections::HashMap;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let mut repos = HashMap::new();
        repos.insert(
            "app".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/app.git".to_string()),
                remote: None,
                path: "app".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: vec![],
                agent: None,
            },
        );
        repos.insert(
            "lib".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/lib.git".to_string()),
                remote: None,
                path: "lib".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: vec![],
                agent: None,
            },
        );

        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos,
            settings: Default::default(),
            workspace: None,
        };

        let filter = vec!["app".to_string()];
        let result = filter_repos(
            &manifest,
            &temp.path().to_path_buf(),
            Some(&filter),
            None,
            false,
        );
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].name, "app");
    }

    #[test]
    fn test_filter_repos_excludes_reference() {
        use crate::core::manifest::Manifest;
        use std::collections::HashMap;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let mut repos = HashMap::new();
        repos.insert(
            "ref-repo".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/ref.git".to_string()),
                remote: None,
                path: "ref".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: true,
                groups: vec![],
                agent: None,
            },
        );
        repos.insert(
            "normal".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/normal.git".to_string()),
                remote: None,
                path: "normal".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: vec![],
                agent: None,
            },
        );

        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos,
            settings: Default::default(),
            workspace: None,
        };

        let result = filter_repos(&manifest, &temp.path().to_path_buf(), None, None, false);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].name, "normal");

        // With include_reference = true
        let result = filter_repos(&manifest, &temp.path().to_path_buf(), None, None, true);
        assert_eq!(result.len(), 2);
    }

    #[test]
    fn test_filter_repos_by_group() {
        use crate::core::manifest::Manifest;
        use std::collections::HashMap;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let mut repos = HashMap::new();
        repos.insert(
            "frontend".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/frontend.git".to_string()),
                remote: None,
                path: "frontend".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: vec!["web".to_string()],
                agent: None,
            },
        );
        repos.insert(
            "backend".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/backend.git".to_string()),
                remote: None,
                path: "backend".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: vec!["api".to_string()],
                agent: None,
            },
        );

        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos,
            settings: Default::default(),
            workspace: None,
        };

        let groups = vec!["web".to_string()];
        let result = filter_repos(
            &manifest,
            &temp.path().to_path_buf(),
            None,
            Some(&groups),
            false,
        );
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].name, "frontend");
    }

    #[test]
    fn test_revision_resolution() {
        use crate::core::manifest::{ManifestSettings, RepoConfig};
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();

        // repo sets revision explicitly
        let config = RepoConfig {
            url: Some("git@github.com:user/app.git".to_string()),
            remote: None,
            path: "app".to_string(),
            revision: Some("develop".to_string()),
            target: None,
            sync_remote: None,
            push_remote: None,
            copyfile: None,
            linkfile: None,
            platform: None,
            reference: false,
            groups: vec![],
            agent: None,
        };
        let settings = ManifestSettings::default();
        let info =
            RepoInfo::from_config("app", &config, &temp.path().to_path_buf(), &settings, None)
                .unwrap();
        assert_eq!(info.revision, "develop");

        // repo omits revision, inherits from settings
        let config = RepoConfig {
            url: Some("git@github.com:user/app.git".to_string()),
            remote: None,
            path: "app".to_string(),
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
        };
        let settings = ManifestSettings {
            revision: Some("master".to_string()),
            ..Default::default()
        };
        let info =
            RepoInfo::from_config("app", &config, &temp.path().to_path_buf(), &settings, None)
                .unwrap();
        assert_eq!(info.revision, "master");

        // both omit → falls back to "main"
        let settings = ManifestSettings::default();
        let info =
            RepoInfo::from_config("app", &config, &temp.path().to_path_buf(), &settings, None)
                .unwrap();
        assert_eq!(info.revision, "main");
    }

    #[test]
    fn test_target_resolution() {
        use crate::core::manifest::{ManifestSettings, RepoConfig};
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let base_config = RepoConfig {
            url: Some("git@github.com:user/app.git".to_string()),
            remote: None,
            path: "app".to_string(),
            revision: Some("main".to_string()),
            target: None,
            sync_remote: None,
            push_remote: None,
            copyfile: None,
            linkfile: None,
            platform: None,
            reference: false,
            groups: vec![],
            agent: None,
        };

        // No target → falls back to revision
        let settings = ManifestSettings::default();
        let info = RepoInfo::from_config(
            "app",
            &base_config,
            &temp.path().to_path_buf(),
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(info.target, "main");
        assert_eq!(info.target_branch(), "main");
        assert_eq!(info.sync_ref(), "origin/main");

        // Global target set
        let settings = ManifestSettings {
            target: Some("develop".to_string()),
            ..Default::default()
        };
        let info = RepoInfo::from_config(
            "app",
            &base_config,
            &temp.path().to_path_buf(),
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(info.target, "develop");
        assert_eq!(info.target_branch(), "develop");

        // Per-repo target overrides global
        let config = RepoConfig {
            target: Some("staging".to_string()),
            ..base_config.clone()
        };
        let settings = ManifestSettings {
            target: Some("develop".to_string()),
            ..Default::default()
        };
        let info =
            RepoInfo::from_config("app", &config, &temp.path().to_path_buf(), &settings, None)
                .unwrap();
        assert_eq!(info.target, "staging");
        assert_eq!(info.target_branch(), "staging");
    }

    #[test]
    fn test_sync_remote_and_push_remote_resolution() {
        use crate::core::manifest::{ManifestSettings, RepoConfig};
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let base_config = RepoConfig {
            url: Some("git@github.com:user/app.git".to_string()),
            remote: None,
            path: "app".to_string(),
            revision: Some("main".to_string()),
            target: None,
            sync_remote: None,
            push_remote: None,
            copyfile: None,
            linkfile: None,
            platform: None,
            reference: false,
            groups: vec![],
            agent: None,
        };

        // Defaults to "origin"
        let settings = ManifestSettings::default();
        let info = RepoInfo::from_config(
            "app",
            &base_config,
            &temp.path().to_path_buf(),
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(info.sync_remote, "origin");
        assert_eq!(info.push_remote, "origin");

        // Settings override
        let settings = ManifestSettings {
            sync_remote: Some("upstream".to_string()),
            push_remote: Some("myfork".to_string()),
            ..Default::default()
        };
        let info = RepoInfo::from_config(
            "app",
            &base_config,
            &temp.path().to_path_buf(),
            &settings,
            None,
        )
        .unwrap();
        assert_eq!(info.sync_remote, "upstream");
        assert_eq!(info.push_remote, "myfork");

        // Per-repo overrides settings
        let config = RepoConfig {
            sync_remote: Some("other".to_string()),
            push_remote: Some("origin".to_string()),
            ..base_config.clone()
        };
        let info =
            RepoInfo::from_config("app", &config, &temp.path().to_path_buf(), &settings, None)
                .unwrap();
        assert_eq!(info.sync_remote, "other");
        assert_eq!(info.push_remote, "origin");
    }

    #[test]
    fn test_url_from_remote() {
        use crate::core::manifest::{ManifestSettings, RemoteConfig, RepoConfig};
        use std::collections::HashMap;
        use tempfile::TempDir;

        let temp = TempDir::new().unwrap();
        let mut remotes = HashMap::new();
        remotes.insert(
            "upstream".to_string(),
            RemoteConfig {
                fetch: "git@github.com:org/".to_string(),
            },
        );

        let config = RepoConfig {
            url: None,
            remote: Some("upstream".to_string()),
            path: "myrepo".to_string(),
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
        };
        let settings = ManifestSettings::default();
        let info = RepoInfo::from_config(
            "myrepo",
            &config,
            &temp.path().to_path_buf(),
            &settings,
            Some(&remotes),
        )
        .unwrap();
        assert_eq!(info.url, "git@github.com:org/myrepo.git");
    }

    #[test]
    fn test_v1_manifest_migration() {
        use crate::core::manifest::Manifest;

        // v1 manifest with default_branch and target in remote/branch format
        let yaml = r#"
version: 1
repos:
  frontend:
    url: git@github.com:org/frontend.git
    path: ./frontend
    target: upstream/develop
  backend:
    url: git@github.com:org/backend.git
    path: ./backend
settings:
  target: develop
  default_branch: master
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        // v1 auto-migrated to v2
        assert_eq!(manifest.version, 2);
        // default_branch alias → revision
        assert_eq!(manifest.settings.revision, Some("master".to_string()));
        // settings target stays as-is (no remote prefix)
        assert_eq!(manifest.settings.target, Some("develop".to_string()));
        // frontend target: "upstream/develop" → target="develop", sync_remote="upstream"
        assert_eq!(
            manifest.repos["frontend"].target,
            Some("develop".to_string())
        );
        assert_eq!(
            manifest.repos["frontend"].sync_remote,
            Some("upstream".to_string())
        );
        // backend has no target
        assert_eq!(manifest.repos["backend"].target, None);
    }

    #[test]
    fn test_v2_manifest_deserialization() {
        use crate::core::manifest::Manifest;

        let yaml = r#"
version: 2
remotes:
  upstream:
    fetch: git@github.com:org/
repos:
  frontend:
    url: git@github.com:me/frontend.git
    path: ./frontend
    target: develop
    sync_remote: upstream
  backend:
    remote: upstream
    path: ./backend
settings:
  revision: main
  target: develop
  push_remote: origin
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.version, 2);
        assert!(manifest.remotes.is_some());
        assert_eq!(manifest.settings.revision, Some("main".to_string()));
        assert_eq!(manifest.settings.target, Some("develop".to_string()));
        assert_eq!(manifest.settings.push_remote, Some("origin".to_string()));
        assert_eq!(
            manifest.repos["frontend"].sync_remote,
            Some("upstream".to_string())
        );
    }

    #[test]
    fn test_backward_compat_no_target() {
        use crate::core::manifest::Manifest;
        use tempfile::TempDir;

        // Existing v1 manifest without target field works unchanged
        let yaml = r#"
repos:
  app:
    url: git@github.com:org/app.git
    path: ./app
    default_branch: main
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let temp = TempDir::new().unwrap();
        let info = RepoInfo::from_config(
            "app",
            &manifest.repos["app"],
            &temp.path().to_path_buf(),
            &manifest.settings,
            manifest.remotes.as_ref(),
        )
        .unwrap();
        assert_eq!(info.revision, "main");
        assert_eq!(info.target, "main");
        assert_eq!(info.target_branch(), "main");
        assert_eq!(info.sync_remote, "origin");
        assert_eq!(info.push_remote, "origin");
    }
}

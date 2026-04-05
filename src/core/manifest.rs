//! Manifest parsing and validation
//!
//! The workspace file (gripspace.yml) defines the multi-repo workspace configuration.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;
use thiserror::Error;

/// Errors that can occur when loading or validating a manifest
#[derive(Error, Debug)]
pub enum ManifestError {
    #[error("Failed to read manifest file: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Failed to parse manifest YAML: {0}")]
    ParseError(#[from] serde_yaml::Error),

    #[error("Validation error: {0}")]
    ValidationError(String),

    #[error("Path escapes workspace boundary: {0}")]
    PathTraversal(String),

    #[error("Gripspace error: {0}")]
    GripspaceError(String),
}

/// Hosting platform type
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum PlatformType {
    #[default]
    #[serde(rename = "github")]
    GitHub,
    #[serde(rename = "gitlab")]
    GitLab,
    #[serde(rename = "azure-devops")]
    AzureDevOps,
    #[serde(rename = "bitbucket")]
    Bitbucket,
}

impl std::fmt::Display for PlatformType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PlatformType::GitHub => write!(f, "github"),
            PlatformType::GitLab => write!(f, "gitlab"),
            PlatformType::AzureDevOps => write!(f, "azure-devops"),
            PlatformType::Bitbucket => write!(f, "bitbucket"),
        }
    }
}

/// Platform configuration for a repository
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlatformConfig {
    #[serde(rename = "type")]
    pub platform_type: PlatformType,
    /// Base URL for self-hosted instances
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
}

/// File copy configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CopyFileConfig {
    /// Source path relative to repo
    pub src: String,
    /// Destination path relative to workspace root
    pub dest: String,
}

/// Symlink configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LinkFileConfig {
    /// Source path relative to repo
    pub src: String,
    /// Destination path relative to workspace root
    pub dest: String,
}

/// Gripspace include configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GripspaceConfig {
    /// Git URL for the gripspace repository
    pub url: String,
    /// Optional revision (branch, tag, or commit SHA) to pin
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rev: Option<String>,
}

/// A part of a composed file
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComposeFilePart {
    /// Source path relative to the gripspace or manifest repo
    pub src: String,
    /// Name of the gripspace to source from (if omitted, sources from local manifest)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gripspace: Option<String>,
}

/// Composed file configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComposeFileConfig {
    /// Destination path relative to workspace root
    pub dest: String,
    /// Ordered parts to concatenate
    pub parts: Vec<ComposeFilePart>,
    /// Separator between parts (default: "\n\n")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub separator: Option<String>,
}

/// Agent context for a repository — build/test/lint commands for AI agents
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RepoAgentConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub build: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub test: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub format: Option<String>,
}

/// A context generation target for an AI tool
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentContextTarget {
    /// Target format: claude, opencode, codex, cursor, raw
    pub format: String,
    /// Destination path relative to workspace root ({repo} placeholder for per-repo generation)
    pub dest: String,
    /// Additional files to append to the generated context
    #[serde(skip_serializing_if = "Option::is_none")]
    pub compose_with: Option<Vec<String>>,
}

/// Agent context for a workspace — conventions and workflows for AI agents
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WorkspaceAgentConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub conventions: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub workflows: Option<HashMap<String, String>>,
    /// Source file for context generation (supports gripspace: prefix)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_source: Option<String>,
    /// Targets for multi-tool context generation
    #[serde(skip_serializing_if = "Option::is_none")]
    pub targets: Option<Vec<AgentContextTarget>>,
}

/// Remote configuration (top-level named remotes with base fetch URLs)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemoteConfig {
    /// Base fetch URL — repo name + ".git" is auto-appended
    pub fetch: String,
}

/// Clone strategy for a repository
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum CloneStrategy {
    /// Standalone git clone (default — best isolation, no shared .git state)
    #[default]
    Clone,
    /// Git worktree off the gripspace root repo (opt-in, experimental)
    Worktree,
}

impl std::fmt::Display for CloneStrategy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CloneStrategy::Clone => write!(f, "clone"),
            CloneStrategy::Worktree => write!(f, "worktree"),
        }
    }
}

/// Repository configuration in the manifest
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoConfig {
    /// Git URL (SSH or HTTPS). Required unless `remote` is set.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    /// Reference to a top-level remote (derives URL from remote's fetch base + repo name)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub remote: Option<String>,
    /// Local path relative to manifest root
    pub path: String,
    /// Upstream revision to sync/clone (e.g. "main", "master"). Inherits from settings.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        alias = "default_branch"
    )]
    pub revision: Option<String>,
    /// PR base branch (just a branch name, e.g. "develop"). Falls back to revision.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
    /// Remote for fetch/rebase operations (e.g. "origin", "upstream"). Inherits from settings.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sync_remote: Option<String>,
    /// Remote for push operations (e.g. "origin"). Inherits from settings.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub push_remote: Option<String>,
    /// Optional file copies
    #[serde(skip_serializing_if = "Option::is_none")]
    pub copyfile: Option<Vec<CopyFileConfig>>,
    /// Optional symlinks
    #[serde(skip_serializing_if = "Option::is_none")]
    pub linkfile: Option<Vec<LinkFileConfig>>,
    /// Optional platform override
    #[serde(skip_serializing_if = "Option::is_none")]
    pub platform: Option<PlatformConfig>,
    /// Reference repo (read-only, excluded from branch/PR operations)
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub reference: bool,
    /// Groups this repo belongs to (for selective operations)
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub groups: Vec<String>,
    /// Agent context metadata (build/test/lint commands for AI agents)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent: Option<RepoAgentConfig>,
    /// Clone strategy: `clone` (default) or `worktree` (opt-in experimental).
    /// Reference repos always use `clone` regardless of this setting.
    /// Inherits from `settings.clone_strategy` if not set.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub clone_strategy: Option<CloneStrategy>,
}

/// Manifest repository self-tracking configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManifestRepoConfig {
    /// Git URL for the manifest repository
    pub url: String,
    /// Upstream revision (inherits from settings.revision if not set)
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        alias = "default_branch"
    )]
    pub revision: Option<String>,
    /// Optional file copies
    #[serde(skip_serializing_if = "Option::is_none")]
    pub copyfile: Option<Vec<CopyFileConfig>>,
    /// Optional symlinks
    #[serde(skip_serializing_if = "Option::is_none")]
    pub linkfile: Option<Vec<LinkFileConfig>>,
    /// Optional composed files (concatenated from gripspace + local parts)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub composefile: Option<Vec<ComposeFileConfig>>,
    /// Optional platform override
    #[serde(skip_serializing_if = "Option::is_none")]
    pub platform: Option<PlatformConfig>,
}

/// PR merge strategy
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum MergeStrategy {
    /// All linked PRs must be merged together or none
    #[default]
    AllOrNothing,
    /// Each PR can be merged independently
    Independent,
}

/// Global manifest settings
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManifestSettings {
    /// PR title prefix (e.g., "[cross-repo]")
    #[serde(default = "default_pr_prefix")]
    pub pr_prefix: String,
    /// Merge strategy for linked PRs
    #[serde(default)]
    pub merge_strategy: MergeStrategy,
    /// Upstream revision for all repos (e.g. "main"). Overridden by per-repo revision.
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        alias = "default_branch"
    )]
    pub revision: Option<String>,
    /// PR base branch for all repos (just a branch name, e.g. "develop").
    /// Overridden by per-repo target. Falls back to revision.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
    /// Remote for fetch/rebase operations (default: "origin"). Overridden by per-repo sync_remote.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sync_remote: Option<String>,
    /// Remote for push operations (default: "origin"). Overridden by per-repo push_remote.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub push_remote: Option<String>,
    /// Default clone strategy for all repos. Per-repo `clone_strategy` overrides this.
    /// Defaults to `clone` if not set.
    #[serde(default)]
    pub clone_strategy: CloneStrategy,
}

fn default_pr_prefix() -> String {
    "[cross-repo]".to_string()
}

impl Default for ManifestSettings {
    fn default() -> Self {
        Self {
            pr_prefix: default_pr_prefix(),
            merge_strategy: MergeStrategy::default(),
            revision: None,
            target: None,
            sync_remote: None,
            push_remote: None,
            clone_strategy: CloneStrategy::default(),
        }
    }
}

/// A step in a multi-step script
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScriptStep {
    /// Step name for display
    pub name: String,
    /// Command to execute
    pub command: String,
    /// Optional working directory
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
}

/// Workspace script definition
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkspaceScript {
    /// Optional description
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// Single command (mutually exclusive with steps)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub command: Option<String>,
    /// Working directory for command
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    /// Multi-step commands (mutually exclusive with command)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub steps: Option<Vec<ScriptStep>>,
}

/// Condition for when a hook should run
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum HookCondition {
    /// Always run the hook
    #[default]
    Always,
    /// Only run if repos had changes during sync
    Changed,
}

/// Hook command definition
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HookCommand {
    /// Command to execute
    pub command: String,
    /// Optional working directory
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    /// Optional display name for the hook
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// Optional list of repos this hook applies to (for condition: changed)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repos: Option<Vec<String>>,
    /// When to run the hook (default: always)
    #[serde(default)]
    pub condition: HookCondition,
}

/// Workspace lifecycle hooks
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WorkspaceHooks {
    /// Hooks to run after sync
    #[serde(rename = "post-sync", skip_serializing_if = "Option::is_none")]
    pub post_sync: Option<Vec<HookCommand>>,
    /// Hooks to run after checkout
    #[serde(rename = "post-checkout", skip_serializing_if = "Option::is_none")]
    pub post_checkout: Option<Vec<HookCommand>>,
}

/// A step in a CI pipeline
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CiStep {
    /// Step name for display
    pub name: String,
    /// Command to execute
    pub command: String,
    /// Optional working directory (relative to workspace root)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    /// Optional environment variables for this step
    #[serde(skip_serializing_if = "Option::is_none")]
    pub env: Option<HashMap<String, String>>,
    /// Continue pipeline even if this step fails
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub continue_on_error: bool,
}

/// A CI pipeline definition
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CiPipeline {
    /// Optional description
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// Steps to execute sequentially
    pub steps: Vec<CiStep>,
}

/// CI/CD configuration in the workspace
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CiConfig {
    /// Named pipelines
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pipelines: Option<HashMap<String, CiPipeline>>,
}

/// A version file to update during release
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VersionFileConfig {
    /// Path relative to workspace root (e.g. "gitgrip/Cargo.toml")
    pub path: String,
    /// Pattern with `{version}` placeholder (e.g. `version = "{version}"`)
    pub pattern: String,
}

/// Release workflow configuration
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ReleaseConfig {
    /// Version files to update (auto-detected if not specified)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version_files: Option<Vec<VersionFileConfig>>,
    /// Path to changelog file (default: "CHANGELOG.md")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub changelog: Option<String>,
    /// Commands to run after release (supports {version} substitution)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub post_release: Option<Vec<HookCommand>>,
}

/// Workspace configuration
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WorkspaceConfig {
    /// Environment variables
    #[serde(skip_serializing_if = "Option::is_none")]
    pub env: Option<HashMap<String, String>>,
    /// Named scripts
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scripts: Option<HashMap<String, WorkspaceScript>>,
    /// Lifecycle hooks
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hooks: Option<WorkspaceHooks>,
    /// CI/CD pipelines
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ci: Option<CiConfig>,
    /// Agent context metadata (conventions and workflows for AI agents)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent: Option<WorkspaceAgentConfig>,
    /// Release workflow configuration
    #[serde(skip_serializing_if = "Option::is_none")]
    pub release: Option<ReleaseConfig>,
}

/// The main manifest structure
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    /// Schema version
    #[serde(default = "default_version")]
    pub version: u32,
    /// Named remotes with base fetch URLs (v2)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub remotes: Option<HashMap<String, RemoteConfig>>,
    /// Gripspace includes (composable manifest inheritance)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gripspaces: Option<Vec<GripspaceConfig>>,
    /// Self-tracking manifest config (optional)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub manifest: Option<ManifestRepoConfig>,
    /// Repository definitions
    pub repos: HashMap<String, RepoConfig>,
    /// Global settings
    #[serde(default)]
    pub settings: ManifestSettings,
    /// Workspace config (optional)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub workspace: Option<WorkspaceConfig>,
}

fn default_version() -> u32 {
    2
}

impl Manifest {
    /// Load a manifest from a YAML file
    pub fn load<P: AsRef<Path>>(path: P) -> Result<Self, ManifestError> {
        let content = std::fs::read_to_string(path)?;
        Self::parse(&content)
    }

    /// Parse a manifest from a YAML string (deserialize only, no validation)
    ///
    /// Use this when you need to process the manifest before validation,
    /// e.g., resolving gripspace includes that merge additional repos.
    pub fn parse_raw(yaml: &str) -> Result<Self, ManifestError> {
        let mut manifest: Manifest = serde_yaml::from_str(yaml)?;
        manifest.migrate_v1();
        Ok(manifest)
    }

    /// Migrate v1 manifests to v2 semantics.
    /// - `default_branch` → `revision` is handled by serde `alias`
    /// - `target` containing "/" is split into (sync_remote, target)
    fn migrate_v1(&mut self) {
        if self.version > 1 {
            return;
        }
        // Migrate settings.target: "origin/develop" → target="develop", sync_remote="origin"
        if let Some(ref target) = self.settings.target {
            if let Some((remote, branch)) = target.split_once('/') {
                if remote != "origin" {
                    self.settings.sync_remote = Some(remote.to_string());
                }
                self.settings.target = Some(branch.to_string());
            }
        }
        // Migrate per-repo targets
        for repo in self.repos.values_mut() {
            // Ensure v1 repos that had a required `url` field get it wrapped in Some
            // (serde alias handles default_branch → revision)
            if let Some(ref target) = repo.target {
                if let Some((remote, branch)) = target.split_once('/') {
                    if remote != "origin" && repo.sync_remote.is_none() {
                        repo.sync_remote = Some(remote.to_string());
                    }
                    repo.target = Some(branch.to_string());
                }
            }
        }
        self.version = 2;
    }

    /// Parse a manifest from a YAML string (deserialize + validate + migrate)
    pub fn parse(yaml: &str) -> Result<Self, ManifestError> {
        let manifest = Self::parse_raw(yaml)?;
        // Note: migrate_v1() is already called in parse_raw()
        manifest.validate()?;
        Ok(manifest)
    }

    /// Resolve the effective clone strategy for a repo.
    ///
    /// Resolution order:
    /// 1. Reference repos always return `Clone` regardless of config
    /// 2. Per-repo `clone_strategy` if set
    /// 3. Global `settings.clone_strategy`
    pub fn effective_clone_strategy(&self, repo: &RepoConfig) -> CloneStrategy {
        if repo.reference {
            return CloneStrategy::Clone;
        }
        repo.clone_strategy
            .unwrap_or(self.settings.clone_strategy)
    }

    /// Validate the manifest
    pub fn validate(&self) -> Result<(), ManifestError> {
        // Must have at least one repo
        if self.repos.is_empty() {
            return Err(ManifestError::ValidationError(
                "Manifest must have at least one repository".to_string(),
            ));
        }

        // Validate each repo config
        for (name, repo) in &self.repos {
            self.validate_repo_config(name, repo)?;
        }

        // Validate manifest repo config if present
        if let Some(ref manifest_config) = self.manifest {
            self.validate_file_configs(
                "manifest",
                &manifest_config.copyfile,
                &manifest_config.linkfile,
            )?;
            if let Some(ref composefiles) = manifest_config.composefile {
                self.validate_composefiles(composefiles)?;
            }
        }

        // Validate gripspace configs
        if let Some(ref gripspaces) = self.gripspaces {
            for gs in gripspaces {
                if gs.url.is_empty() {
                    return Err(ManifestError::ValidationError(
                        "Gripspace has empty URL".to_string(),
                    ));
                }
            }
        }

        // Validate workspace scripts
        if let Some(ref workspace) = self.workspace {
            self.validate_workspace_config(workspace)?;
        }

        Ok(())
    }

    /// Lint the manifest for portability issues (warnings, not errors).
    /// Returns a list of warning messages about absolute paths in hooks,
    /// scripts, and env values. (#418)
    pub fn lint_absolute_paths(&self) -> Vec<String> {
        let mut warnings = Vec::new();

        if let Some(ref workspace) = self.workspace {
            // Check hooks (post-sync and post-checkout)
            if let Some(ref hooks) = workspace.hooks {
                let all_hooks = hooks
                    .post_sync
                    .iter()
                    .flatten()
                    .chain(hooks.post_checkout.iter().flatten());
                for hook in all_hooks {
                    if hook.command.starts_with('/') || is_windows_absolute(&hook.command) {
                        warnings.push(format!(
                            "Hook command contains absolute path: {}",
                            hook.command.chars().take(80).collect::<String>()
                        ));
                    }
                }
            }

            // Check env values
            if let Some(ref env) = workspace.env {
                for (key, val) in env {
                    if val.starts_with('/') || is_windows_absolute(val) {
                        warnings.push(format!(
                            "Env var {} contains absolute path: {}",
                            key,
                            val.chars().take(80).collect::<String>()
                        ));
                    }
                }
            }
        }

        warnings
    }

    /// Validate a gripspace manifest (allows empty repos since gripspaces may only contribute
    /// scripts, hooks, env, or file configs).
    pub fn validate_as_gripspace(&self) -> Result<(), ManifestError> {
        // Validate each repo config (if any)
        for (name, repo) in &self.repos {
            self.validate_repo_config(name, repo)?;
        }

        // Validate manifest config if present
        if let Some(ref manifest_config) = self.manifest {
            self.validate_file_configs(
                "manifest",
                &manifest_config.copyfile,
                &manifest_config.linkfile,
            )?;
            if let Some(ref composefiles) = manifest_config.composefile {
                self.validate_composefiles(composefiles)?;
            }
        }

        // Validate gripspace configs
        if let Some(ref gripspaces) = self.gripspaces {
            for gs in gripspaces {
                if gs.url.is_empty() {
                    return Err(ManifestError::ValidationError(
                        "Gripspace has empty URL".to_string(),
                    ));
                }
            }
        }

        // Validate workspace scripts
        if let Some(ref workspace) = self.workspace {
            self.validate_workspace_config(workspace)?;
        }

        Ok(())
    }

    fn validate_repo_config(&self, name: &str, repo: &RepoConfig) -> Result<(), ManifestError> {
        // Must have either url or remote
        let has_url = repo.url.as_ref().is_some_and(|u| !u.is_empty());
        let has_remote = repo.remote.as_ref().is_some_and(|r| !r.is_empty());
        if !has_url && !has_remote {
            return Err(ManifestError::ValidationError(format!(
                "Repository '{}' must have either a 'url' or 'remote'",
                name
            )));
        }

        // If remote is set, verify it exists in top-level remotes
        if let Some(ref remote_name) = repo.remote {
            if !remote_name.is_empty() {
                let remote_exists = self
                    .remotes
                    .as_ref()
                    .is_some_and(|r| r.contains_key(remote_name));
                if !remote_exists {
                    return Err(ManifestError::ValidationError(format!(
                        "Repository '{}' references remote '{}' which is not defined in top-level remotes",
                        name, remote_name
                    )));
                }
            }
        }

        // Path must be non-empty
        if repo.path.is_empty() {
            return Err(ManifestError::ValidationError(format!(
                "Repository '{}' must have a path",
                name
            )));
        }

        // Validate path doesn't escape boundary
        if path_escapes_boundary(&repo.path) {
            return Err(ManifestError::PathTraversal(format!(
                "Repository '{}' path escapes workspace boundary: {}",
                name, repo.path
            )));
        }

        // Reference repos must use clone strategy — reject worktree
        if repo.reference
            && repo
                .clone_strategy
                .is_some_and(|s| s == CloneStrategy::Worktree)
        {
            return Err(ManifestError::ValidationError(format!(
                "Repository '{}' is a reference repo and cannot use clone_strategy 'worktree'",
                name
            )));
        }

        // Validate copyfile/linkfile configs
        self.validate_file_configs(name, &repo.copyfile, &repo.linkfile)?;

        Ok(())
    }

    fn validate_file_configs(
        &self,
        repo_name: &str,
        copyfile: &Option<Vec<CopyFileConfig>>,
        linkfile: &Option<Vec<LinkFileConfig>>,
    ) -> Result<(), ManifestError> {
        if let Some(ref copyfiles) = copyfile {
            for cf in copyfiles {
                if cf.src.is_empty() || cf.dest.is_empty() {
                    return Err(ManifestError::ValidationError(format!(
                        "Repository '{}' has copyfile with empty src or dest",
                        repo_name
                    )));
                }
                if path_escapes_boundary(&cf.src) {
                    return Err(ManifestError::PathTraversal(format!(
                        "Repository '{}' copyfile src escapes boundary: {}",
                        repo_name, cf.src
                    )));
                }
                if path_escapes_boundary(&cf.dest) {
                    return Err(ManifestError::PathTraversal(format!(
                        "Repository '{}' copyfile dest escapes boundary: {}",
                        repo_name, cf.dest
                    )));
                }
            }
        }

        if let Some(ref linkfiles) = linkfile {
            for lf in linkfiles {
                if lf.src.is_empty() || lf.dest.is_empty() {
                    return Err(ManifestError::ValidationError(format!(
                        "Repository '{}' has linkfile with empty src or dest",
                        repo_name
                    )));
                }
                if path_escapes_boundary(&lf.src) {
                    return Err(ManifestError::PathTraversal(format!(
                        "Repository '{}' linkfile src escapes boundary: {}",
                        repo_name, lf.src
                    )));
                }
                if path_escapes_boundary(&lf.dest) {
                    return Err(ManifestError::PathTraversal(format!(
                        "Repository '{}' linkfile dest escapes boundary: {}",
                        repo_name, lf.dest
                    )));
                }
            }
        }

        Ok(())
    }

    fn validate_workspace_config(&self, workspace: &WorkspaceConfig) -> Result<(), ManifestError> {
        if let Some(ref scripts) = workspace.scripts {
            for (name, script) in scripts {
                // Scripts must have either command or steps, not both
                match (&script.command, &script.steps) {
                    (Some(_), Some(_)) => {
                        return Err(ManifestError::ValidationError(format!(
                            "Script '{}' cannot have both 'command' and 'steps'",
                            name
                        )));
                    }
                    (None, None) => {
                        return Err(ManifestError::ValidationError(format!(
                            "Script '{}' must have either 'command' or 'steps'",
                            name
                        )));
                    }
                    (None, Some(steps)) => {
                        // Validate each step
                        for step in steps {
                            if step.name.is_empty() {
                                return Err(ManifestError::ValidationError(format!(
                                    "Script '{}' has a step with empty name",
                                    name
                                )));
                            }
                            if step.command.is_empty() {
                                return Err(ManifestError::ValidationError(format!(
                                    "Script '{}' step '{}' has empty command",
                                    name, step.name
                                )));
                            }
                        }
                    }
                    (Some(_), None) => {
                        // Single command is valid
                    }
                }
            }
        }

        Ok(())
    }

    fn validate_composefiles(
        &self,
        composefiles: &[ComposeFileConfig],
    ) -> Result<(), ManifestError> {
        for cf in composefiles {
            if cf.dest.is_empty() {
                return Err(ManifestError::ValidationError(
                    "Composefile has empty dest".to_string(),
                ));
            }
            if path_escapes_boundary(&cf.dest) {
                return Err(ManifestError::PathTraversal(format!(
                    "Composefile dest escapes boundary: {}",
                    cf.dest
                )));
            }
            if cf.parts.is_empty() {
                return Err(ManifestError::ValidationError(format!(
                    "Composefile '{}' has no parts",
                    cf.dest
                )));
            }
            for part in &cf.parts {
                if part.src.is_empty() {
                    return Err(ManifestError::ValidationError(format!(
                        "Composefile '{}' has a part with empty src",
                        cf.dest
                    )));
                }
                if path_escapes_boundary(&part.src) {
                    return Err(ManifestError::PathTraversal(format!(
                        "Composefile '{}' part src escapes boundary: {}",
                        cf.dest, part.src
                    )));
                }
                if let Some(ref gs_name) = part.gripspace {
                    if gs_name.is_empty() {
                        return Err(ManifestError::ValidationError(format!(
                            "Composefile '{}' has a part with empty gripspace name",
                            cf.dest
                        )));
                    }
                    if gs_name.contains("..") || gs_name.contains('/') || gs_name.contains('\\') {
                        return Err(ManifestError::PathTraversal(format!(
                            "Composefile '{}' gripspace name contains invalid characters: {}",
                            cf.dest, gs_name
                        )));
                    }
                }
            }
        }

        Ok(())
    }
}

fn is_windows_absolute(path: &str) -> bool {
    let bytes = path.as_bytes();
    (bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':')
        || path.starts_with("\\\\")
}

/// Check if a path escapes the workspace boundary
fn path_escapes_boundary(path: &str) -> bool {
    // Normalize path separators
    let normalized = path.replace('\\', "/");

    // Reject: paths starting with "..", "/", containing "/../", ending with "/..",
    // or Windows absolute/UNC paths.
    if normalized.starts_with("..")
        || normalized.starts_with('/')
        || normalized.contains("/../")
        || normalized.ends_with("/..")
        || is_windows_absolute(path)
    {
        return true;
    }

    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_minimal_manifest() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.repos.len(), 1);
        assert!(manifest.repos.contains_key("myrepo"));
    }

    #[test]
    fn test_parse_full_manifest() {
        let yaml = r#"
version: 1
manifest:
  url: git@github.com:user/manifest.git
  default_branch: main
repos:
  app:
    url: git@github.com:user/app.git
    path: app
    default_branch: main
    copyfile:
      - src: README.md
        dest: APP_README.md
    linkfile:
      - src: config.yaml
        dest: app-config.yaml
settings:
  pr_prefix: "[multi-repo]"
  merge_strategy: all-or-nothing
workspace:
  env:
    NODE_ENV: development
  scripts:
    build:
      description: Build all packages
      command: npm run build
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.version, 2); // v1 auto-migrated to v2
        assert!(manifest.manifest.is_some());
        assert_eq!(manifest.repos.len(), 1);
        assert_eq!(manifest.settings.pr_prefix, "[multi-repo]");
    }

    #[test]
    fn test_empty_repos_fails() {
        let yaml = r#"
repos: {}
"#;
        let result = Manifest::parse(yaml);
        assert!(result.is_err());
    }

    #[test]
    fn test_path_traversal_fails() {
        let yaml = r#"
repos:
  evil:
    url: git@github.com:user/repo.git
    path: ../outside
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::PathTraversal(_))));
    }

    #[test]
    fn test_absolute_path_fails() {
        let yaml = r#"
repos:
  evil:
    url: git@github.com:user/repo.git
    path: /etc/passwd
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::PathTraversal(_))));
    }

    #[test]
    fn test_script_with_both_command_and_steps_fails() {
        let yaml = r#"
repos:
  app:
    url: git@github.com:user/app.git
    path: app
workspace:
  scripts:
    bad:
      command: echo hello
      steps:
        - name: step1
          command: echo step
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_path_escapes_boundary() {
        assert!(path_escapes_boundary(".."));
        assert!(path_escapes_boundary("../foo"));
        assert!(path_escapes_boundary("/etc"));
        assert!(path_escapes_boundary("foo/../../../etc"));
        assert!(path_escapes_boundary("foo/.."));
        assert!(path_escapes_boundary("foo/bar/.."));
        assert!(path_escapes_boundary("C:\\Windows\\System32\\drivers\\etc"));
        assert!(path_escapes_boundary("C:/Windows/System32/drivers/etc"));
        assert!(path_escapes_boundary("C:relative\\path"));
        assert!(path_escapes_boundary("\\\\server\\share\\folder"));
        assert!(!path_escapes_boundary("foo"));
        assert!(!path_escapes_boundary("foo/bar"));
        assert!(!path_escapes_boundary("./foo"));
    }

    #[test]
    fn test_reference_repos() {
        let yaml = r#"
repos:
  main-repo:
    url: git@github.com:user/main.git
    path: main
  ref-repo:
    url: https://github.com/other/reference.git
    path: ./ref/reference
    reference: true
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.repos.len(), 2);

        let main_repo = manifest.repos.get("main-repo").unwrap();
        assert!(!main_repo.reference);

        let ref_repo = manifest.repos.get("ref-repo").unwrap();
        assert!(ref_repo.reference);
    }

    #[test]
    fn test_manifest_groups_parse() {
        let yaml = r#"
repos:
  frontend:
    url: git@github.com:user/frontend.git
    path: frontend
    groups: [core, ui]
  backend:
    url: git@github.com:user/backend.git
    path: backend
    groups: [core, api]
  docs:
    url: git@github.com:user/docs.git
    path: docs
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.repos.len(), 3);

        let frontend = manifest.repos.get("frontend").unwrap();
        assert_eq!(frontend.groups, vec!["core", "ui"]);

        let backend = manifest.repos.get("backend").unwrap();
        assert_eq!(backend.groups, vec!["core", "api"]);

        let docs = manifest.repos.get("docs").unwrap();
        assert!(docs.groups.is_empty());
    }

    #[test]
    fn test_repos_without_groups_default_empty() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let repo = manifest.repos.get("myrepo").unwrap();
        assert!(repo.groups.is_empty());
    }

    #[test]
    fn test_reference_default_false() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let repo = manifest.repos.get("myrepo").unwrap();
        assert!(!repo.reference); // Should default to false
    }

    #[test]
    fn test_parse_gripspaces() {
        let yaml = r#"
gripspaces:
  - url: https://github.com/user/base-gripspace.git
    rev: main
  - url: https://github.com/user/other-gripspace.git
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let gripspaces = manifest.gripspaces.unwrap();
        assert_eq!(gripspaces.len(), 2);
        assert_eq!(
            gripspaces[0].url,
            "https://github.com/user/base-gripspace.git"
        );
        assert_eq!(gripspaces[0].rev.as_deref(), Some("main"));
        assert_eq!(
            gripspaces[1].url,
            "https://github.com/user/other-gripspace.git"
        );
        assert!(gripspaces[1].rev.is_none());
    }

    #[test]
    fn test_parse_composefile() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: CLAUDE.md
      parts:
        - gripspace: base-gripspace
          src: CODI.md
        - src: PRIVATE_DOCS.md
      separator: "\n\n---\n\n"
    - dest: envsetup.sh
      parts:
        - gripspace: base-gripspace
          src: envsetup.sh
        - src: private-envsetup.sh
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let manifest_config = manifest.manifest.unwrap();
        let composefiles = manifest_config.composefile.unwrap();
        assert_eq!(composefiles.len(), 2);

        let cf1 = &composefiles[0];
        assert_eq!(cf1.dest, "CLAUDE.md");
        assert_eq!(cf1.parts.len(), 2);
        assert_eq!(cf1.parts[0].gripspace.as_deref(), Some("base-gripspace"));
        assert_eq!(cf1.parts[0].src, "CODI.md");
        assert!(cf1.parts[1].gripspace.is_none());
        assert_eq!(cf1.parts[1].src, "PRIVATE_DOCS.md");
        assert_eq!(cf1.separator.as_deref(), Some("\n\n---\n\n"));

        let cf2 = &composefiles[1];
        assert_eq!(cf2.dest, "envsetup.sh");
        assert_eq!(cf2.parts.len(), 2);
    }

    #[test]
    fn test_parse_raw_does_not_validate() {
        let yaml = r#"
repos: {}
"#;
        // parse_raw should succeed even with empty repos (no validation)
        let manifest = Manifest::parse_raw(yaml).unwrap();
        assert!(manifest.repos.is_empty());

        // parse should fail with validation
        let result = Manifest::parse(yaml);
        assert!(result.is_err());
    }

    #[test]
    fn test_gripspace_empty_url_fails() {
        let yaml = r#"
gripspaces:
  - url: ""
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_composefile_empty_dest_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: ""
      parts:
        - src: file.md
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_composefile_empty_parts_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: output.md
      parts: []
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_composefile_path_traversal_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: ../outside.md
      parts:
        - src: file.md
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::PathTraversal(_))));
    }

    #[test]
    fn test_composefile_gripspace_name_traversal_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: output.md
      parts:
        - gripspace: "../evil"
          src: file.md
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::PathTraversal(_))));
    }

    #[test]
    fn test_composefile_empty_gripspace_name_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: output.md
      parts:
        - gripspace: ""
          src: file.md
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_validate_as_gripspace_composefile_empty_parts_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: output.md
      parts: []
repos: {}
"#;
        let manifest = Manifest::parse_raw(yaml).unwrap();
        let result = manifest.validate_as_gripspace();
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_validate_as_gripspace_composefile_invalid_gripspace_name_fails() {
        let yaml = r#"
manifest:
  url: git@github.com:user/manifest.git
  composefile:
    - dest: output.md
      parts:
        - gripspace: "../evil"
          src: file.md
repos: {}
"#;
        let manifest = Manifest::parse_raw(yaml).unwrap();
        let result = manifest.validate_as_gripspace();
        assert!(matches!(result, Err(ManifestError::PathTraversal(_))));
    }

    #[test]
    fn test_parse_repo_agent_config() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
    agent:
      description: "Rust CLI tool"
      language: rust
      build: "cargo build"
      test: "cargo test"
      lint: "cargo clippy"
      format: "cargo fmt"
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let repo = manifest.repos.get("myrepo").unwrap();
        let agent = repo.agent.as_ref().unwrap();
        assert_eq!(agent.description.as_deref(), Some("Rust CLI tool"));
        assert_eq!(agent.language.as_deref(), Some("rust"));
        assert_eq!(agent.build.as_deref(), Some("cargo build"));
        assert_eq!(agent.test.as_deref(), Some("cargo test"));
        assert_eq!(agent.lint.as_deref(), Some("cargo clippy"));
        assert_eq!(agent.format.as_deref(), Some("cargo fmt"));
    }

    #[test]
    fn test_parse_workspace_agent_config() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
workspace:
  agent:
    description: "Multi-repo workspace"
    conventions:
      - "Use conventional commits"
      - "All PRs require review"
    workflows:
      deploy: "./scripts/deploy.sh"
      release: "gr run release"
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let workspace = manifest.workspace.as_ref().unwrap();
        let agent = workspace.agent.as_ref().unwrap();
        assert_eq!(agent.description.as_deref(), Some("Multi-repo workspace"));
        assert_eq!(agent.conventions.len(), 2);
        assert_eq!(agent.conventions[0], "Use conventional commits");
        assert_eq!(agent.conventions[1], "All PRs require review");
        let workflows = agent.workflows.as_ref().unwrap();
        assert_eq!(workflows.get("deploy").unwrap(), "./scripts/deploy.sh");
        assert_eq!(workflows.get("release").unwrap(), "gr run release");
    }

    #[test]
    fn test_agent_config_optional() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let repo = manifest.repos.get("myrepo").unwrap();
        assert!(repo.agent.is_none());
        assert!(manifest.workspace.is_none());
    }

    #[test]
    fn test_agent_config_serialization_roundtrip() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
    agent:
      description: "Test repo"
      language: rust
      build: "cargo build"
      test: "cargo test"
workspace:
  agent:
    description: "Test workspace"
    conventions:
      - "convention one"
    workflows:
      deploy: "deploy.sh"
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let serialized = serde_yaml::to_string(&manifest).unwrap();
        let reparsed = Manifest::parse(&serialized).unwrap();

        let orig_agent = manifest
            .repos
            .get("myrepo")
            .unwrap()
            .agent
            .as_ref()
            .unwrap();
        let re_agent = reparsed
            .repos
            .get("myrepo")
            .unwrap()
            .agent
            .as_ref()
            .unwrap();
        assert_eq!(orig_agent.description, re_agent.description);
        assert_eq!(orig_agent.language, re_agent.language);
        assert_eq!(orig_agent.build, re_agent.build);
        assert_eq!(orig_agent.test, re_agent.test);

        let orig_ws_agent = manifest.workspace.as_ref().unwrap().agent.as_ref().unwrap();
        let re_ws_agent = reparsed.workspace.as_ref().unwrap().agent.as_ref().unwrap();
        assert_eq!(orig_ws_agent.description, re_ws_agent.description);
        assert_eq!(orig_ws_agent.conventions, re_ws_agent.conventions);
        assert_eq!(orig_ws_agent.workflows, re_ws_agent.workflows);
    }

    #[test]
    fn test_repo_agent_config_partial() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
    agent:
      language: python
      test: "pytest"
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let repo = manifest.repos.get("myrepo").unwrap();
        let agent = repo.agent.as_ref().unwrap();
        assert!(agent.description.is_none());
        assert_eq!(agent.language.as_deref(), Some("python"));
        assert!(agent.build.is_none());
        assert_eq!(agent.test.as_deref(), Some("pytest"));
        assert!(agent.lint.is_none());
        assert!(agent.format.is_none());
    }

    #[test]
    fn test_manifest_with_no_gripspaces() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert!(manifest.gripspaces.is_none());
    }

    #[test]
    fn test_clone_strategy_defaults_to_clone() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        let repo = &manifest.repos["myrepo"];
        assert_eq!(manifest.settings.clone_strategy, CloneStrategy::Clone);
        assert!(repo.clone_strategy.is_none());
        assert_eq!(
            manifest.effective_clone_strategy(repo),
            CloneStrategy::Clone
        );
    }

    #[test]
    fn test_clone_strategy_per_repo_override() {
        let yaml = r#"
repos:
  cloned:
    url: git@github.com:user/a.git
    path: a
  worktree:
    url: git@github.com:user/b.git
    path: b
    clone_strategy: worktree
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(
            manifest.effective_clone_strategy(&manifest.repos["cloned"]),
            CloneStrategy::Clone
        );
        assert_eq!(
            manifest.effective_clone_strategy(&manifest.repos["worktree"]),
            CloneStrategy::Worktree
        );
    }

    #[test]
    fn test_clone_strategy_global_override() {
        let yaml = r#"
repos:
  myrepo:
    url: git@github.com:user/repo.git
    path: repo
settings:
  clone_strategy: worktree
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.settings.clone_strategy, CloneStrategy::Worktree);
        assert_eq!(
            manifest.effective_clone_strategy(&manifest.repos["myrepo"]),
            CloneStrategy::Worktree
        );
    }

    #[test]
    fn test_reference_repo_always_clone() {
        let yaml = r#"
repos:
  refonly:
    url: https://github.com/other/repo.git
    path: reference/repo
    reference: true
    clone_strategy: worktree
settings:
  clone_strategy: worktree
"#;
        // Validation must reject worktree on reference repos
        let result = Manifest::parse(yaml);
        assert!(matches!(result, Err(ManifestError::ValidationError(_))));
    }

    #[test]
    fn test_reference_repo_effective_strategy_always_clone() {
        let yaml = r#"
repos:
  refonly:
    url: https://github.com/other/repo.git
    path: reference/repo
    reference: true
settings:
  clone_strategy: worktree
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        // Even though global default is worktree, reference repos resolve to clone
        assert_eq!(
            manifest.effective_clone_strategy(&manifest.repos["refonly"]),
            CloneStrategy::Clone
        );
    }

    #[test]
    fn test_clone_strategy_backward_compat_no_field() {
        // Existing manifests without clone_strategy should parse and default to clone
        let yaml = r#"
version: 2
repos:
  synapt:
    url: git@github.com:user/synapt.git
    path: ./synapt
    revision: main
settings:
  pr_prefix: "[cross-repo]"
  merge_strategy: independent
"#;
        let manifest = Manifest::parse(yaml).unwrap();
        assert_eq!(manifest.settings.clone_strategy, CloneStrategy::Clone);
        assert_eq!(
            manifest.effective_clone_strategy(&manifest.repos["synapt"]),
            CloneStrategy::Clone
        );
    }

    #[test]
    fn test_lint_catches_absolute_paths_in_hooks() {
        let yaml = r#"
repos:
  myrepo:
    url: https://github.com/test/repo.git
    path: ./myrepo
workspace:
  hooks:
    post-sync:
      - command: "/usr/local/bin/setup.sh"
    post-checkout:
      - command: "echo ok"
  env:
    HOME_DIR: "/Users/layne/Development"
"#;
        let manifest: Manifest = serde_yaml::from_str(yaml).unwrap();
        let warnings = manifest.lint_absolute_paths();
        assert_eq!(warnings.len(), 2);
        assert!(warnings[0].contains("Hook command"));
        assert!(warnings[1].contains("Env var"));
    }

    #[test]
    fn test_lint_no_warnings_for_relative_paths() {
        let yaml = r#"
repos:
  myrepo:
    url: https://github.com/test/repo.git
    path: ./myrepo
workspace:
  hooks:
    post-sync:
      - command: "./scripts/setup.sh"
  env:
    PROJECT: "myproject"
"#;
        let manifest: Manifest = serde_yaml::from_str(yaml).unwrap();
        let warnings = manifest.lint_absolute_paths();
        assert!(warnings.is_empty());
    }
}

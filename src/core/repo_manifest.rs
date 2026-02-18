//! git-repo XML manifest parser
//!
//! Parses Google's `repo` tool XML manifest format (`default.xml`) and converts
//! it to a gitgrip `Manifest`. Gerrit remotes (those with a `review` attribute)
//! are skipped — only non-Gerrit repos get PR capabilities.

use quick_xml::de::from_str;
use serde::Deserialize;
use std::collections::HashMap;
use std::path::Path;
use thiserror::Error;

use crate::core::manifest::{
    CopyFileConfig, LinkFileConfig, Manifest, ManifestSettings, PlatformConfig, PlatformType,
    RepoConfig, WorkspaceConfig,
};
use crate::platform;

/// Errors from parsing repo manifests
#[derive(Error, Debug)]
pub enum RepoManifestError {
    #[error("Failed to read XML file: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Failed to parse XML: {0}")]
    XmlParseError(String),

    #[error("Missing remote: project references remote '{0}' which is not defined")]
    MissingRemote(String),

    #[error("No default remote defined and project '{0}' has no remote attribute")]
    NoDefaultRemote(String),

    #[error("Failed to resolve include: {0}")]
    IncludeError(String),
}

// ── XML types ──────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize, Clone)]
#[serde(rename = "manifest")]
pub struct XmlManifest {
    #[serde(rename = "remote", default)]
    pub remotes: Vec<XmlRemote>,

    #[serde(rename = "default", default)]
    pub default: Option<XmlDefault>,

    #[serde(rename = "project", default)]
    pub projects: Vec<XmlProject>,

    #[serde(rename = "include", default)]
    pub includes: Vec<XmlInclude>,

    #[serde(rename = "remove-project", default)]
    pub remove_projects: Vec<XmlRemoveProject>,

    #[serde(rename = "extend-project", default)]
    pub extend_projects: Vec<XmlExtendProject>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlRemote {
    #[serde(rename = "@name")]
    pub name: String,

    #[serde(rename = "@fetch")]
    pub fetch: String,

    /// If present, this remote uses Gerrit for code review
    #[serde(rename = "@review", default)]
    pub review: Option<String>,

    #[serde(rename = "@revision", default)]
    pub revision: Option<String>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlDefault {
    #[serde(rename = "@remote", default)]
    pub remote: Option<String>,

    #[serde(rename = "@revision", default)]
    pub revision: Option<String>,

    #[serde(rename = "@sync-j", default)]
    pub sync_j: Option<String>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlProject {
    #[serde(rename = "@name")]
    pub name: String,

    #[serde(rename = "@path", default)]
    pub path: Option<String>,

    #[serde(rename = "@remote", default)]
    pub remote: Option<String>,

    #[serde(rename = "@revision", default)]
    pub revision: Option<String>,

    #[serde(rename = "@groups", default)]
    pub groups: Option<String>,

    #[serde(rename = "@clone-depth", default)]
    pub clone_depth: Option<String>,

    #[serde(rename = "copyfile", default)]
    pub copyfiles: Vec<XmlCopyFile>,

    #[serde(rename = "linkfile", default)]
    pub linkfiles: Vec<XmlLinkFile>,

    /// Nested sub-projects
    #[serde(rename = "project", default)]
    pub sub_projects: Vec<XmlProject>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlCopyFile {
    #[serde(rename = "@src")]
    pub src: String,

    #[serde(rename = "@dest")]
    pub dest: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlLinkFile {
    #[serde(rename = "@src")]
    pub src: String,

    #[serde(rename = "@dest")]
    pub dest: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlInclude {
    #[serde(rename = "@name")]
    pub name: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlRemoveProject {
    #[serde(rename = "@name")]
    pub name: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct XmlExtendProject {
    #[serde(rename = "@name")]
    pub name: String,

    #[serde(rename = "@path", default)]
    pub path: Option<String>,

    #[serde(rename = "@remote", default)]
    pub remote: Option<String>,

    #[serde(rename = "@revision", default)]
    pub revision: Option<String>,

    #[serde(rename = "@groups", default)]
    pub groups: Option<String>,
}

// ── Conversion result ──────────────────────────────────────────────────────

/// Result of converting an XML manifest to a gitgrip manifest
#[derive(Debug)]
pub struct ConversionResult {
    /// The converted gitgrip manifest
    pub manifest: Manifest,
    /// Number of Gerrit repos that were skipped
    pub gerrit_skipped: usize,
    /// Number of non-Gerrit repos that were imported
    pub non_gerrit_imported: usize,
    /// Total projects in XML
    pub total_projects: usize,
    /// Platform breakdown
    pub platform_counts: HashMap<PlatformType, usize>,
}

// ── Implementation ─────────────────────────────────────────────────────────

impl XmlManifest {
    /// Parse from an XML string
    pub fn parse(xml: &str) -> Result<Self, RepoManifestError> {
        from_str(xml).map_err(|e| RepoManifestError::XmlParseError(e.to_string()))
    }

    /// Parse from a file, resolving `<include>` elements relative to the file's directory
    pub fn parse_file(path: &Path) -> Result<Self, RepoManifestError> {
        // Follow symlinks
        let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        let content = std::fs::read_to_string(&resolved)?;
        let mut manifest = Self::parse(&content)?;

        // Resolve includes
        let base_dir = resolved.parent().unwrap_or_else(|| Path::new("."));
        manifest.resolve_includes(base_dir)?;

        Ok(manifest)
    }

    /// Resolve `<include>` elements by loading referenced XML files
    fn resolve_includes(&mut self, base_dir: &Path) -> Result<(), RepoManifestError> {
        if self.includes.is_empty() {
            return Ok(());
        }

        let includes = std::mem::take(&mut self.includes);

        for include in &includes {
            let include_path = base_dir.join(&include.name);
            if !include_path.exists() {
                return Err(RepoManifestError::IncludeError(format!(
                    "Include file not found: {}",
                    include_path.display()
                )));
            }

            let content = std::fs::read_to_string(&include_path).map_err(|e| {
                RepoManifestError::IncludeError(format!(
                    "Failed to read {}: {}",
                    include_path.display(),
                    e
                ))
            })?;

            let included: XmlManifest = Self::parse(&content)?;

            // Merge included manifest into this one
            self.remotes.extend(included.remotes);
            self.projects.extend(included.projects);
            self.remove_projects.extend(included.remove_projects);
            self.extend_projects.extend(included.extend_projects);

            // Default is not merged - the main manifest's default takes precedence
        }

        Ok(())
    }

    /// Convert to a gitgrip Manifest, skipping Gerrit remotes
    pub fn to_manifest(&self) -> Result<ConversionResult, RepoManifestError> {
        // Build remote map
        let remote_map: HashMap<&str, &XmlRemote> =
            self.remotes.iter().map(|r| (r.name.as_str(), r)).collect();

        // Identify Gerrit remotes
        let gerrit_remotes: std::collections::HashSet<&str> = self
            .remotes
            .iter()
            .filter(|r| r.review.is_some())
            .map(|r| r.name.as_str())
            .collect();

        // Get default remote and revision
        let default_remote = self.default.as_ref().and_then(|d| d.remote.as_deref());
        let default_revision = self.default.as_ref().and_then(|d| d.revision.as_deref());

        // Build set of removed projects
        let removed: std::collections::HashSet<&str> = self
            .remove_projects
            .iter()
            .map(|r| r.name.as_str())
            .collect();

        // Build extend overrides map
        let extends: HashMap<&str, &XmlExtendProject> = self
            .extend_projects
            .iter()
            .map(|e| (e.name.as_str(), e))
            .collect();

        // Flatten projects (including nested sub-projects)
        let all_projects = self.flatten_projects();

        let mut repos = HashMap::new();
        let mut gerrit_skipped = 0;
        let mut non_gerrit_imported = 0;
        let mut platform_counts: HashMap<PlatformType, usize> = HashMap::new();
        let total_projects = all_projects.len();

        for project in &all_projects {
            // Skip removed projects
            if removed.contains(project.name.as_str()) {
                continue;
            }

            // Apply extend overrides
            let project = self.apply_extend(project, &extends);

            // Resolve remote
            let remote_name = project
                .remote
                .as_deref()
                .or(default_remote)
                .ok_or_else(|| RepoManifestError::NoDefaultRemote(project.name.clone()))?;

            let remote = remote_map
                .get(remote_name)
                .ok_or_else(|| RepoManifestError::MissingRemote(remote_name.to_string()))?;

            // Skip Gerrit remotes
            if gerrit_remotes.contains(remote_name) {
                gerrit_skipped += 1;
                continue;
            }

            // Resolve revision
            let revision = project
                .revision
                .as_deref()
                .or(remote.revision.as_deref())
                .or(default_revision)
                .unwrap_or("main");

            // Compute default branch from revision
            let default_branch = revision_to_branch(revision);

            // Compute URL
            let fetch_base = remote.fetch.trim_end_matches('/');
            let url = if fetch_base.is_empty() || fetch_base == "." {
                project.name.clone()
            } else {
                let name_part = &project.name;
                let full_url = format!("{}/{}", fetch_base, name_part);
                if full_url.ends_with(".git") {
                    full_url
                } else {
                    format!("{}.git", full_url)
                }
            };

            // Compute path
            let path = project.path.as_deref().unwrap_or(&project.name).to_string();

            // Parse groups
            let groups: Vec<String> = project
                .groups
                .as_deref()
                .map(|g| g.split(',').map(|s| s.trim().to_string()).collect())
                .unwrap_or_default();

            // notdefault group → reference: true
            let reference = groups.iter().any(|g| g == "notdefault");

            // Convert copyfile/linkfile
            let copyfile: Option<Vec<CopyFileConfig>> = if project.copyfiles.is_empty() {
                None
            } else {
                Some(
                    project
                        .copyfiles
                        .iter()
                        .map(|cf| CopyFileConfig {
                            src: cf.src.clone(),
                            dest: cf.dest.clone(),
                        })
                        .collect(),
                )
            };

            let linkfile: Option<Vec<LinkFileConfig>> = if project.linkfiles.is_empty() {
                None
            } else {
                Some(
                    project
                        .linkfiles
                        .iter()
                        .map(|lf| LinkFileConfig {
                            src: lf.src.clone(),
                            dest: lf.dest.clone(),
                        })
                        .collect(),
                )
            };

            // Detect platform
            let platform_type = platform::detect_platform(&url);
            *platform_counts.entry(platform_type).or_insert(0) += 1;

            // Generate a manifest-safe name from the project name
            let repo_name = project_name_to_key(&project.name);

            repos.insert(
                repo_name,
                RepoConfig {
                    url,
                    path,
                    default_branch: Some(default_branch),
                    target: None,
                    copyfile,
                    linkfile,
                    platform: Some(PlatformConfig {
                        platform_type,
                        base_url: None,
                    }),
                    reference,
                    groups,
                    agent: None,
                },
            );

            non_gerrit_imported += 1;
        }

        let manifest = Manifest {
            version: 1,
            gripspaces: None,
            manifest: None,
            repos,
            settings: ManifestSettings::default(),
            workspace: Some(WorkspaceConfig::default()),
        };

        Ok(ConversionResult {
            manifest,
            gerrit_skipped,
            non_gerrit_imported,
            total_projects,
            platform_counts,
        })
    }

    /// Flatten nested projects into a single list
    fn flatten_projects(&self) -> Vec<XmlProject> {
        let mut result = Vec::new();
        for project in &self.projects {
            Self::flatten_project(project, &mut result);
        }
        result
    }

    fn flatten_project(project: &XmlProject, result: &mut Vec<XmlProject>) {
        // Add the project itself (without sub-projects)
        let mut flat = project.clone();
        flat.sub_projects = Vec::new();
        result.push(flat);

        // Recurse into sub-projects
        for sub in &project.sub_projects {
            Self::flatten_project(sub, result);
        }
    }

    /// Apply extend-project overrides to a project
    fn apply_extend(
        &self,
        project: &XmlProject,
        extends: &HashMap<&str, &XmlExtendProject>,
    ) -> XmlProject {
        if let Some(ext) = extends.get(project.name.as_str()) {
            let mut p = project.clone();
            if let Some(ref path) = ext.path {
                p.path = Some(path.clone());
            }
            if let Some(ref remote) = ext.remote {
                p.remote = Some(remote.clone());
            }
            if let Some(ref revision) = ext.revision {
                p.revision = Some(revision.clone());
            }
            if let Some(ref groups) = ext.groups {
                p.groups = Some(groups.clone());
            }
            p
        } else {
            project.clone()
        }
    }
}

/// Convert a revision string to a branch name
/// e.g., "refs/heads/main" -> "main", "main" -> "main"
fn revision_to_branch(revision: &str) -> String {
    if let Some(branch) = revision.strip_prefix("refs/heads/") {
        branch.to_string()
    } else if let Some(tag) = revision.strip_prefix("refs/tags/") {
        tag.to_string()
    } else {
        revision.to_string()
    }
}

/// Convert a project name to a valid manifest key
/// e.g., "platform/frameworks/base" -> "platform-frameworks-base"
fn project_name_to_key(name: &str) -> String {
    name.replace(['/', ' '], "-")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_minimal_xml() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <default remote="origin" revision="main" />
  <project name="app" path="app" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        assert_eq!(manifest.remotes.len(), 1);
        assert_eq!(manifest.projects.len(), 1);
        assert_eq!(manifest.projects[0].name, "app");
    }

    #[test]
    fn test_parse_full_xml() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="github" fetch="https://github.com/org" />
  <remote name="bb" fetch="https://bitbucket.org/team" />
  <default remote="github" revision="main" />
  <project name="frontend" path="frontend" remote="github" groups="core,ui">
    <copyfile src="Makefile" dest="Makefile" />
    <linkfile src="config.yaml" dest="frontend-config.yaml" />
  </project>
  <project name="backend" path="backend" remote="bb" groups="core,api" />
  <project name="docs" path="docs" groups="notdefault" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        assert_eq!(manifest.remotes.len(), 2);
        assert_eq!(manifest.projects.len(), 3);
        assert_eq!(manifest.projects[0].copyfiles.len(), 1);
        assert_eq!(manifest.projects[0].linkfiles.len(), 1);
    }

    #[test]
    fn test_url_resolution() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/myorg" />
  <default remote="origin" revision="main" />
  <project name="myrepo" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        let repo = result.manifest.repos.values().next().unwrap();
        assert_eq!(repo.url, "https://github.com/myorg/myrepo.git");
    }

    #[test]
    fn test_groups_parsing() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <default remote="origin" revision="main" />
  <project name="app" groups="core, ui, frontend" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        let repo = result.manifest.repos.values().next().unwrap();
        assert_eq!(repo.groups, vec!["core", "ui", "frontend"]);
    }

    #[test]
    fn test_notdefault_becomes_reference() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <default remote="origin" revision="main" />
  <project name="tools" groups="notdefault" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        let repo = result.manifest.repos.values().next().unwrap();
        assert!(repo.reference);
    }

    #[test]
    fn test_gerrit_repos_skipped() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="aosp" fetch="https://android.googlesource.com" review="https://android-review.googlesource.com/" />
  <remote name="github" fetch="https://github.com/org" />
  <default remote="aosp" revision="main" />
  <project name="platform/build" />
  <project name="external/tool" remote="github" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        assert_eq!(result.gerrit_skipped, 1);
        assert_eq!(result.non_gerrit_imported, 1);
        assert_eq!(result.manifest.repos.len(), 1);
        assert!(result.manifest.repos.contains_key("external-tool"));
    }

    #[test]
    fn test_remove_project() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <default remote="origin" revision="main" />
  <project name="app" />
  <project name="deprecated" />
  <remove-project name="deprecated" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        assert_eq!(result.manifest.repos.len(), 1);
        assert!(result.manifest.repos.contains_key("app"));
    }

    #[test]
    fn test_extend_project() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <remote name="fork" fetch="https://github.com/fork" />
  <default remote="origin" revision="main" />
  <project name="app" path="app" />
  <extend-project name="app" remote="fork" revision="develop" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        let repo = result.manifest.repos.get("app").unwrap();
        assert_eq!(repo.url, "https://github.com/fork/app.git");
        assert_eq!(repo.default_branch, Some("develop".to_string()));
    }

    #[test]
    fn test_conversion_to_manifest() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="github" fetch="https://github.com/myorg" />
  <default remote="github" revision="refs/heads/main" />
  <project name="frontend" path="frontend" />
  <project name="backend" path="backend" revision="develop" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();

        assert_eq!(result.manifest.repos.len(), 2);
        assert_eq!(result.non_gerrit_imported, 2);
        assert_eq!(result.gerrit_skipped, 0);

        let frontend = result.manifest.repos.get("frontend").unwrap();
        assert_eq!(frontend.default_branch, Some("main".to_string()));
        assert_eq!(frontend.url, "https://github.com/myorg/frontend.git");

        let backend = result.manifest.repos.get("backend").unwrap();
        assert_eq!(backend.default_branch, Some("develop".to_string()));
    }

    #[test]
    fn test_missing_remote_error() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="app" remote="nonexistent" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest();
        assert!(result.is_err());
        match result.unwrap_err() {
            RepoManifestError::MissingRemote(name) => assert_eq!(name, "nonexistent"),
            e => panic!("Expected MissingRemote, got: {:?}", e),
        }
    }

    #[test]
    fn test_multiple_platforms() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="github" fetch="https://github.com/org" />
  <remote name="bb" fetch="https://bitbucket.org/team" />
  <default remote="github" revision="main" />
  <project name="app" />
  <project name="infra" remote="bb" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        assert_eq!(result.non_gerrit_imported, 2);
        assert!(result.platform_counts.contains_key(&PlatformType::GitHub));
        assert!(result
            .platform_counts
            .contains_key(&PlatformType::Bitbucket));
    }

    #[test]
    fn test_include_resolution() {
        use std::fs;
        let temp = tempfile::TempDir::new().unwrap();

        // Write main manifest
        let main_xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <default remote="origin" revision="main" />
  <project name="app" />
  <include name="extra.xml" />
</manifest>"#;
        let main_path = temp.path().join("default.xml");
        fs::write(&main_path, main_xml).unwrap();

        // Write included manifest
        let extra_xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <project name="lib" />
</manifest>"#;
        fs::write(temp.path().join("extra.xml"), extra_xml).unwrap();

        let manifest = XmlManifest::parse_file(&main_path).unwrap();
        let result = manifest.to_manifest().unwrap();
        assert_eq!(result.manifest.repos.len(), 2);
        assert!(result.manifest.repos.contains_key("app"));
        assert!(result.manifest.repos.contains_key("lib"));
    }

    #[test]
    fn test_revision_to_branch() {
        assert_eq!(revision_to_branch("refs/heads/main"), "main");
        assert_eq!(revision_to_branch("refs/tags/v1.0"), "v1.0");
        assert_eq!(revision_to_branch("develop"), "develop");
        assert_eq!(revision_to_branch("main"), "main");
    }

    #[test]
    fn test_project_name_to_key() {
        assert_eq!(project_name_to_key("app"), "app");
        assert_eq!(
            project_name_to_key("platform/frameworks/base"),
            "platform-frameworks-base"
        );
    }

    #[test]
    fn test_no_default_remote_error() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="origin" fetch="https://github.com/org" />
  <project name="app" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest();
        assert!(result.is_err());
    }

    #[test]
    fn test_mixed_gerrit_and_github() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="gerrit" fetch="https://gerrit.example.com" review="https://gerrit.example.com" />
  <remote name="github" fetch="https://github.com/org" />
  <default remote="gerrit" revision="main" />
  <project name="core/framework" />
  <project name="core/lib" />
  <project name="tools/build" remote="github" />
  <project name="tools/test" remote="github" />
</manifest>"#;

        let manifest = XmlManifest::parse(xml).unwrap();
        let result = manifest.to_manifest().unwrap();
        assert_eq!(result.total_projects, 4);
        assert_eq!(result.gerrit_skipped, 2);
        assert_eq!(result.non_gerrit_imported, 2);
    }
}

//! Test fixtures for creating workspace environments.
//!
//! Provides a `WorkspaceBuilder` pattern for creating temporary workspaces
//! with configurable repos, bare remotes, and manifest files -- all offline.

use std::fs;
use std::path::{Path, PathBuf};
use tempfile::TempDir;

use gitgrip::core::griptree::GriptreeConfig;

use super::git_helpers;

/// A test workspace with temporary directories that are cleaned up on drop.
pub struct WorkspaceFixture {
    /// The temporary directory (holds both workspace and bare remotes).
    /// Kept alive for the lifetime of the fixture.
    pub _temp: TempDir,
    /// Path to the workspace root (contains .gitgrip/, repos).
    pub workspace_root: PathBuf,
    /// Path to the bare remotes directory.
    pub remotes_dir: PathBuf,
    /// Names of repos that were created.
    pub repo_names: Vec<String>,
    /// Names of reference repos.
    pub reference_repos: Vec<String>,
}

impl WorkspaceFixture {
    /// Get the path to a repo within the workspace.
    pub fn repo_path(&self, name: &str) -> PathBuf {
        self.workspace_root.join(name)
    }

    /// Get the path to a bare remote.
    pub fn remote_path(&self, name: &str) -> PathBuf {
        self.remotes_dir.join(format!("{}.git", name))
    }

    /// Get the file:// URL for a bare remote.
    pub fn remote_url(&self, name: &str) -> String {
        format!("file://{}", self.remote_path(name).display())
    }

    /// Load the manifest from this workspace.
    pub fn load_manifest(&self) -> gitgrip::core::manifest::Manifest {
        let manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(&self.workspace_root)
                .expect("workspace manifest path should resolve");
        let content = fs::read_to_string(&manifest_path).unwrap_or_else(|e| {
            panic!(
                "failed to read manifest at {}: {}",
                manifest_path.display(),
                e
            )
        });
        gitgrip::core::manifest::Manifest::parse(&content).expect("failed to parse manifest")
    }
}

/// Write a minimal griptree config with a single repo upstream mapping.
pub fn write_griptree_config(workspace_root: &Path, branch: &str, repo: &str, upstream: &str) {
    let mut config = GriptreeConfig::new(branch, &workspace_root.to_string_lossy());
    config
        .repo_upstreams
        .insert(repo.to_string(), upstream.to_string());
    let config_path = workspace_root.join(".gitgrip").join("griptree.json");
    config.save(&config_path).unwrap();
}

/// Builder for creating test workspaces.
pub struct WorkspaceBuilder {
    repos: Vec<RepoSpec>,
    with_manifest_repo: bool,
}

struct RepoSpec {
    name: String,
    reference: bool,
    /// If true, create a bare remote and clone from it (enables push/pull).
    with_remote: bool,
    /// Extra files to commit during setup.
    files: Vec<(String, String)>,
    /// Groups this repo belongs to.
    groups: Vec<String>,
}

impl WorkspaceBuilder {
    pub fn new() -> Self {
        Self {
            repos: Vec::new(),
            with_manifest_repo: false,
        }
    }

    /// Add a regular repo.
    pub fn add_repo(mut self, name: &str) -> Self {
        self.repos.push(RepoSpec {
            name: name.to_string(),
            reference: false,
            with_remote: true,
            files: vec![("README.md".to_string(), format!("# {}\n", name))],
            groups: Vec::new(),
        });
        self
    }

    /// Add a regular repo with group tags.
    pub fn add_repo_with_groups(mut self, name: &str, groups: Vec<&str>) -> Self {
        self.repos.push(RepoSpec {
            name: name.to_string(),
            reference: false,
            with_remote: true,
            files: vec![("README.md".to_string(), format!("# {}\n", name))],
            groups: groups.into_iter().map(|g| g.to_string()).collect(),
        });
        self
    }

    /// Add a reference repo (read-only, excluded from branch/PR ops).
    pub fn add_reference_repo(mut self, name: &str) -> Self {
        self.repos.push(RepoSpec {
            name: name.to_string(),
            reference: true,
            with_remote: true,
            files: vec![("README.md".to_string(), format!("# {} (reference)\n", name))],
            groups: Vec::new(),
        });
        self
    }

    /// Add a repo with specific initial files.
    pub fn add_repo_with_files(mut self, name: &str, files: Vec<(&str, &str)>) -> Self {
        self.repos.push(RepoSpec {
            name: name.to_string(),
            reference: false,
            with_remote: true,
            files: files
                .into_iter()
                .map(|(n, c)| (n.to_string(), c.to_string()))
                .collect(),
            groups: Vec::new(),
        });
        self
    }

    /// Include a manifest repo (as a git repo itself).
    pub fn with_manifest_repo(mut self) -> Self {
        self.with_manifest_repo = true;
        self
    }

    /// Build the workspace fixture.
    pub fn build(self) -> WorkspaceFixture {
        let temp = TempDir::new().expect("failed to create temp dir");
        let workspace_root = temp.path().join("workspace");
        let remotes_dir = temp.path().join("remotes");
        fs::create_dir_all(&workspace_root).unwrap();
        fs::create_dir_all(&remotes_dir).unwrap();

        let mut repo_names = Vec::new();
        let mut reference_repos = Vec::new();

        // Create bare remotes and clone into workspace
        for spec in &self.repos {
            let bare_path = remotes_dir.join(format!("{}.git", spec.name));
            let repo_path = workspace_root.join(&spec.name);

            // Create bare remote
            git_helpers::init_bare_repo(&bare_path);

            // Create a temporary staging repo, add files, push to bare
            let staging = temp.path().join(format!("staging-{}", spec.name));
            git_helpers::init_repo(&staging);

            for (filename, content) in &spec.files {
                git_helpers::commit_file(&staging, filename, content, &format!("Add {}", filename));
            }

            // Add bare as remote and push
            let remote_url = format!("file://{}", bare_path.display());
            git_helpers::add_remote(&staging, "origin", &remote_url);
            git_helpers::push_upstream(&staging, "origin", "main");

            // Clone from bare into workspace
            git_helpers::clone_repo(&remote_url, &repo_path);

            if spec.reference {
                reference_repos.push(spec.name.clone());
            }
            repo_names.push(spec.name.clone());
        }

        // Generate manifest YAML
        let mut manifest_yaml = generate_manifest(&self.repos, &remotes_dir);

        let manifest_dir = workspace_root.join(".gitgrip").join("spaces").join("main");

        // Self-tracking `manifest:` block (core::manifest::ManifestRepoConfig),
        // required by get_manifest_repo_info -- without it, `manifest.manifest`
        // stays None and any operation that includes the "manifest" pseudo-repo
        // (e.g. `gr checkout add --repo <x>,manifest`) silently drops it, even
        // though `with_manifest_repo` already made it a real git repo on disk.
        if self.with_manifest_repo {
            manifest_yaml.push_str(&format!(
                "manifest:\n  url: file://{}\n",
                manifest_dir.display()
            ));
        }

        // Write canonical manifest layout.
        fs::create_dir_all(&manifest_dir).unwrap();
        fs::write(manifest_dir.join("gripspace.yml"), &manifest_yaml).unwrap();

        // Keep legacy mirror for tests that still reference .gitgrip/manifests.
        let legacy_manifest_dir = workspace_root.join(".gitgrip").join("manifests");
        fs::create_dir_all(&legacy_manifest_dir).unwrap();
        fs::write(legacy_manifest_dir.join("manifest.yaml"), &manifest_yaml).unwrap();

        // Create local overlay directory for future local space tests.
        fs::create_dir_all(workspace_root.join(".gitgrip").join("spaces").join("local")).unwrap();

        // Optionally init the manifests dir as a git repo
        if self.with_manifest_repo {
            git_helpers::init_repo(&manifest_dir);
            git_helpers::commit_file(
                &manifest_dir,
                "gripspace.yml",
                &manifest_yaml,
                "Initial manifest",
            );
        }

        WorkspaceFixture {
            _temp: temp,
            workspace_root,
            remotes_dir,
            repo_names,
            reference_repos,
        }
    }
}

/// Generate manifest YAML from repo specs.
///
/// Uses fake github.com URLs so that `RepoInfo::from_config` can parse them
/// (the URL parser doesn't handle `file://` URLs). The actual git remote in
/// each cloned repo still points to the local bare remote via `file://`.
fn generate_manifest(repos: &[RepoSpec], remotes_dir: &Path) -> String {
    let mut yaml = String::from("version: 1\nrepos:\n");

    for spec in repos {
        let remote_url = format!(
            "file://{}",
            remotes_dir.join(format!("{}.git", spec.name)).display()
        );
        yaml.push_str(&format!("  {}:\n", spec.name));
        yaml.push_str(&format!("    url: {}\n", remote_url));
        yaml.push_str(&format!("    path: {}\n", spec.name));
        yaml.push_str("    default_branch: main\n");
        if spec.reference {
            yaml.push_str("    reference: true\n");
        }
        if !spec.groups.is_empty() {
            let groups_str: Vec<String> =
                spec.groups.iter().map(|g| format!("\"{}\"", g)).collect();
            yaml.push_str(&format!("    groups: [{}]\n", groups_str.join(", ")));
        }
    }

    yaml
}

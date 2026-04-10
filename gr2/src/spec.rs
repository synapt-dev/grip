use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

pub const WORKSPACE_SPEC_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkspaceSpec {
    pub schema_version: u32,
    pub workspace_name: String,
    pub cache: CacheSpec,
    #[serde(default)]
    pub repos: Vec<RepoSpec>,
    #[serde(default)]
    pub units: Vec<UnitSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CacheSpec {
    pub root: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepoSpec {
    pub name: String,
    pub path: String,
    pub url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct UnitSpec {
    pub name: String,
    pub path: String,
    #[serde(default)]
    pub repos: Vec<String>,
}

impl WorkspaceSpec {
    pub fn from_workspace(workspace_root: &Path) -> Result<Self> {
        let workspace_name = read_workspace_name(workspace_root)?;
        let repos = read_registered_repos(workspace_root)?;
        let units = read_registered_units(workspace_root)?;

        Ok(Self {
            schema_version: WORKSPACE_SPEC_VERSION,
            workspace_name,
            cache: CacheSpec {
                root: ".grip/cache".to_string(),
            },
            repos,
            units,
        })
    }

    pub fn validate_for_plan(&self) -> Result<()> {
        if self.schema_version != WORKSPACE_SPEC_VERSION {
            anyhow::bail!(
                "unsupported workspace spec schema_version {}: expected {}",
                self.schema_version,
                WORKSPACE_SPEC_VERSION
            );
        }

        if self.workspace_name.trim().is_empty() {
            anyhow::bail!("workspace spec workspace_name must not be empty");
        }

        let mut repo_names = HashSet::new();
        for repo in &self.repos {
            if !repo_names.insert(repo.name.clone()) {
                anyhow::bail!("workspace spec contains duplicate repo '{}'", repo.name);
            }

            if repo.path.trim().is_empty() || repo.url.trim().is_empty() {
                anyhow::bail!("repo '{}' must include non-empty path and url", repo.name);
            }
        }

        let mut unit_names = HashSet::new();
        for unit in &self.units {
            if !unit_names.insert(unit.name.clone()) {
                anyhow::bail!("workspace spec contains duplicate unit '{}'", unit.name);
            }

            if unit.path.trim().is_empty() {
                anyhow::bail!("unit '{}' must include a non-empty path", unit.name);
            }

            for repo_name in &unit.repos {
                if !repo_names.contains(repo_name) {
                    anyhow::bail!(
                        "unit '{}' references missing repo '{}'",
                        unit.name,
                        repo_name
                    );
                }
            }
        }

        Ok(())
    }

    pub fn validate(&self, workspace_root: &Path) -> Result<()> {
        self.validate_for_plan()?;

        for repo in &self.repos {
            let repo_root = workspace_root.join(&repo.path);
            if !repo_root.join("repo.toml").exists() {
                anyhow::bail!(
                    "workspace spec repo '{}' is missing repo metadata at {}",
                    repo.name,
                    repo_root.join("repo.toml").display()
                );
            }
        }

        for unit in &self.units {
            let unit_root = workspace_root.join(&unit.path);
            if !unit_root.join("unit.toml").exists() {
                anyhow::bail!(
                    "workspace spec unit '{}' is missing unit metadata at {}",
                    unit.name,
                    unit_root.join("unit.toml").display()
                );
            }
        }

        Ok(())
    }
}

pub fn write_workspace_spec(workspace_root: &Path, spec: &WorkspaceSpec) -> Result<PathBuf> {
    let spec_path = workspace_spec_path(workspace_root);
    let content = toml::to_string_pretty(spec).context("serialize workspace spec")?;
    fs::write(&spec_path, content)
        .with_context(|| format!("write workspace spec to {}", spec_path.display()))?;
    Ok(spec_path)
}

pub fn read_workspace_spec(workspace_root: &Path) -> Result<WorkspaceSpec> {
    let spec_path = workspace_spec_path(workspace_root);
    let content = fs::read_to_string(&spec_path)
        .with_context(|| format!("read workspace spec from {}", spec_path.display()))?;
    toml::from_str(&content).context("parse workspace spec")
}

pub fn workspace_spec_path(workspace_root: &Path) -> PathBuf {
    workspace_root.join(".grip/workspace_spec.toml")
}

fn read_workspace_name(workspace_root: &Path) -> Result<String> {
    let workspace_toml = fs::read_to_string(workspace_root.join(".grip/workspace.toml"))
        .context("read .grip/workspace.toml")?;
    workspace_toml
        .lines()
        .find_map(|line| line.strip_prefix("name = \""))
        .and_then(|line| line.strip_suffix('"'))
        .map(str::to_owned)
        .context("workspace name missing from .grip/workspace.toml")
}

fn read_registered_repos(workspace_root: &Path) -> Result<Vec<RepoSpec>> {
    let repos_root = workspace_root.join("repos");
    let mut repos = Vec::new();

    for entry in fs::read_dir(&repos_root)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }

        let repo_root = entry.path();
        let repo_toml = repo_root.join("repo.toml");
        if !repo_toml.exists() {
            continue;
        }

        let content = fs::read_to_string(&repo_toml)?;
        let fallback_name = entry.file_name().to_string_lossy().into_owned();
        let name = content
            .lines()
            .find_map(|line| line.strip_prefix("name = \""))
            .and_then(|line| line.strip_suffix('"'))
            .map(str::to_owned)
            .unwrap_or(fallback_name.clone());
        let url = content
            .lines()
            .find_map(|line| line.strip_prefix("url = \""))
            .and_then(|line| line.strip_suffix('"'))
            .unwrap_or("")
            .to_string();

        repos.push(RepoSpec {
            name,
            path: format!("repos/{}", fallback_name),
            url,
        });
    }

    repos.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(repos)
}

fn read_registered_units(workspace_root: &Path) -> Result<Vec<UnitSpec>> {
    let units_root = workspace_root.join("agents");
    let mut units = Vec::new();

    if !units_root.exists() {
        return Ok(units);
    }

    for entry in fs::read_dir(&units_root)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }

        let unit_root = entry.path();
        let unit_toml = unit_root.join("unit.toml");
        if !unit_toml.exists() {
            continue;
        }

        let fallback_name = entry.file_name().to_string_lossy().into_owned();
        units.push(UnitSpec {
            name: fallback_name.clone(),
            path: format!("agents/{}", fallback_name),
            repos: Vec::new(),
        });
    }

    units.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(units)
}

//! Target command implementation
//!
//! View and set the PR target branch (base branch) globally or per-repo.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use crate::core::repo::RepoInfo;
use serde_yaml::Value;
use std::path::{Path, PathBuf};

/// Show current target branches for all repos
pub fn run_target_list(workspace_root: &Path, manifest: &Manifest) -> anyhow::Result<()> {
    Output::header("Target Branches");
    println!();

    let global_target = manifest.settings.target.as_deref();
    let global_revision = manifest.settings.revision.as_deref();

    println!(
        "  Global target: {}",
        if let Some(t) = global_target {
            Output::repo_name(t).to_string()
        } else {
            "(not set)".to_string()
        }
    );
    if let Some(rev) = global_revision {
        println!("  Global revision: {}", rev);
    }
    println!();

    let repos: Vec<RepoInfo> = manifest
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
        .collect();

    // Find repos with per-repo target overrides
    let mut has_overrides = false;
    for (name, config) in &manifest.repos {
        if config.target.is_some() {
            if !has_overrides {
                Output::subheader("Per-repo overrides:");
                has_overrides = true;
            }
            println!(
                "  {}: {}",
                name,
                config.target.as_deref().unwrap_or("(none)")
            );
        }
    }
    if has_overrides {
        println!();
    }

    Output::subheader("Effective targets:");
    for repo in &repos {
        println!("  {}: {}", repo.name, repo.target_branch());
    }
    println!();

    Ok(())
}

/// Set the global target branch
pub fn run_target_set(workspace_root: &Path, branch: &str) -> anyhow::Result<()> {
    let manifest_path = find_manifest_path(workspace_root)?;
    let content = std::fs::read_to_string(&manifest_path)?;
    let mut manifest: Value = serde_yaml::from_str(&content)?;

    let settings = manifest
        .get_mut("settings")
        .and_then(|s| s.as_mapping_mut());

    match settings {
        Some(settings_map) => {
            settings_map.insert(
                Value::String("target".to_string()),
                Value::String(branch.to_string()),
            );
        }
        None => {
            // Create settings section
            let mut settings_map = serde_yaml::Mapping::new();
            settings_map.insert(
                Value::String("target".to_string()),
                Value::String(branch.to_string()),
            );
            manifest
                .as_mapping_mut()
                .ok_or_else(|| anyhow::anyhow!("Manifest root is not a mapping"))?
                .insert(
                    Value::String("settings".to_string()),
                    Value::Mapping(settings_map),
                );
        }
    }

    let yaml = serde_yaml::to_string(&manifest)?;
    std::fs::write(&manifest_path, &yaml)?;
    manifest_paths::sync_legacy_mirror_if_present(workspace_root, &manifest_path, &yaml)?;

    Output::success(&format!("Global target set to '{}'", branch));
    Ok(())
}

/// Set the target branch for a specific repo
pub fn run_target_set_repo(
    workspace_root: &Path,
    repo_name: &str,
    branch: &str,
) -> anyhow::Result<()> {
    let manifest_path = find_manifest_path(workspace_root)?;
    let content = std::fs::read_to_string(&manifest_path)?;
    let mut manifest: Value = serde_yaml::from_str(&content)?;

    // Verify repo exists in resolved manifest
    let resolved = Manifest::load(&manifest_path)?;
    if !resolved.repos.contains_key(repo_name) {
        anyhow::bail!("Repository '{}' not found in workspace", repo_name);
    }

    let repos_section = manifest
        .get_mut("repos")
        .ok_or_else(|| anyhow::anyhow!("No 'repos' section found in manifest"))?
        .as_mapping_mut()
        .ok_or_else(|| anyhow::anyhow!("'repos' is not a mapping"))?;

    let repo_key = Value::String(repo_name.to_string());
    let repo_entry = repos_section
        .get_mut(&repo_key)
        .ok_or_else(|| anyhow::anyhow!("Repository '{}' not found in local manifest", repo_name))?
        .as_mapping_mut()
        .ok_or_else(|| anyhow::anyhow!("Repository '{}' is not a mapping", repo_name))?;

    repo_entry.insert(
        Value::String("target".to_string()),
        Value::String(branch.to_string()),
    );

    let yaml = serde_yaml::to_string(&manifest)?;
    std::fs::write(&manifest_path, &yaml)?;
    manifest_paths::sync_legacy_mirror_if_present(workspace_root, &manifest_path, &yaml)?;

    Output::success(&format!("{}: target set to '{}'", repo_name, branch));
    Ok(())
}

/// Unset the global target (falls back to revision)
pub fn run_target_unset(workspace_root: &Path) -> anyhow::Result<()> {
    let manifest_path = find_manifest_path(workspace_root)?;
    let content = std::fs::read_to_string(&manifest_path)?;
    let mut manifest: Value = serde_yaml::from_str(&content)?;

    if let Some(settings) = manifest
        .get_mut("settings")
        .and_then(|s| s.as_mapping_mut())
    {
        settings.remove(Value::String("target".to_string()));
    }

    let yaml = serde_yaml::to_string(&manifest)?;
    std::fs::write(&manifest_path, &yaml)?;
    manifest_paths::sync_legacy_mirror_if_present(workspace_root, &manifest_path, &yaml)?;

    Output::success("Global target unset (will fall back to revision)");
    Ok(())
}

/// Unset the target for a specific repo (falls back to global)
pub fn run_target_unset_repo(workspace_root: &Path, repo_name: &str) -> anyhow::Result<()> {
    let manifest_path = find_manifest_path(workspace_root)?;
    let content = std::fs::read_to_string(&manifest_path)?;
    let mut manifest: Value = serde_yaml::from_str(&content)?;

    // Verify repo exists
    let resolved = Manifest::load(&manifest_path)?;
    if !resolved.repos.contains_key(repo_name) {
        anyhow::bail!("Repository '{}' not found in workspace", repo_name);
    }

    let repos_section = manifest
        .get_mut("repos")
        .ok_or_else(|| anyhow::anyhow!("No 'repos' section found in manifest"))?
        .as_mapping_mut()
        .ok_or_else(|| anyhow::anyhow!("'repos' is not a mapping"))?;

    let repo_key = Value::String(repo_name.to_string());
    if let Some(repo_entry) = repos_section
        .get_mut(&repo_key)
        .and_then(|v| v.as_mapping_mut())
    {
        repo_entry.remove(Value::String("target".to_string()));
    }

    let yaml = serde_yaml::to_string(&manifest)?;
    std::fs::write(&manifest_path, &yaml)?;
    manifest_paths::sync_legacy_mirror_if_present(workspace_root, &manifest_path, &yaml)?;

    Output::success(&format!(
        "{}: target unset (will fall back to global)",
        repo_name
    ));
    Ok(())
}

fn find_manifest_path(workspace_root: &Path) -> anyhow::Result<PathBuf> {
    if let Some(path) = manifest_paths::resolve_gripspace_manifest_path(workspace_root) {
        return Ok(path);
    }
    if let Some(path) = manifest_paths::resolve_repo_manifest_path(workspace_root) {
        return Ok(path);
    }

    anyhow::bail!(
        "No workspace manifest found in .gitgrip/spaces/main, .gitgrip/manifests, or .repo/manifests"
    )
}

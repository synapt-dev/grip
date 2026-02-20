//! Link command implementation
//!
//! Manages copyfile and linkfile entries.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use crate::core::repo::RepoInfo;
use crate::files::{process_composefiles, resolve_file_source};
use crate::git::path_exists;
use std::path::PathBuf;

/// Run the link command
pub fn run_link(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    status: bool,
    apply: bool,
    json: bool,
) -> anyhow::Result<()> {
    if status {
        show_link_status(workspace_root, manifest, json)?;
    } else if apply {
        apply_links(workspace_root, manifest, false)?;
    } else {
        // Default: show status
        show_link_status(workspace_root, manifest, json)?;
    }

    Ok(())
}

fn show_link_status(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    json: bool,
) -> anyhow::Result<()> {
    if !json {
        Output::header("File Link Status");
        println!();
    }

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

    let mut total_links = 0;
    let mut valid_links = 0;
    let mut broken_links = 0;

    #[derive(serde::Serialize)]
    struct JsonLink {
        link_type: String,
        src: String,
        dest: String,
        status: String,
    }
    let mut json_links: Vec<JsonLink> = Vec::new();

    for (name, config) in &manifest.repos {
        let repo = repos.iter().find(|r| &r.name == name);

        // Check copyfiles
        if let Some(ref copyfiles) = config.copyfile {
            for copyfile in copyfiles {
                total_links += 1;
                let source = repo
                    .map(|r| r.absolute_path.join(&copyfile.src))
                    .unwrap_or_else(|| workspace_root.join(&config.path).join(&copyfile.src));
                let dest = workspace_root.join(&copyfile.dest);

                let status = if source.exists() && dest.exists() {
                    valid_links += 1;
                    "valid"
                } else if !source.exists() {
                    broken_links += 1;
                    "broken: source missing"
                } else {
                    broken_links += 1;
                    "broken: dest missing"
                };

                if json {
                    json_links.push(JsonLink {
                        link_type: "copyfile".to_string(),
                        src: copyfile.src.clone(),
                        dest: copyfile.dest.clone(),
                        status: status.to_string(),
                    });
                } else {
                    let symbol = if status == "valid" {
                        "✓"
                    } else {
                        &format!("✗ ({})", status.strip_prefix("broken: ").unwrap_or(status))
                    };
                    println!("  [copy] {} -> {} {}", copyfile.src, copyfile.dest, symbol);
                }
            }
        }

        // Check linkfiles
        if let Some(ref linkfiles) = config.linkfile {
            for linkfile in linkfiles {
                total_links += 1;
                let source = repo
                    .map(|r| r.absolute_path.join(&linkfile.src))
                    .unwrap_or_else(|| workspace_root.join(&config.path).join(&linkfile.src));
                let dest = workspace_root.join(&linkfile.dest);

                let status = if source.exists() && dest.exists() && dest.is_symlink() {
                    valid_links += 1;
                    "valid"
                } else if !source.exists() {
                    broken_links += 1;
                    "broken: source missing"
                } else if !dest.exists() {
                    broken_links += 1;
                    "broken: link missing"
                } else {
                    broken_links += 1;
                    "broken: not a symlink"
                };

                if json {
                    json_links.push(JsonLink {
                        link_type: "linkfile".to_string(),
                        src: linkfile.src.clone(),
                        dest: linkfile.dest.clone(),
                        status: status.to_string(),
                    });
                } else {
                    let symbol = if status == "valid" {
                        "✓"
                    } else {
                        &format!("✗ ({})", status.strip_prefix("broken: ").unwrap_or(status))
                    };
                    println!("  [link] {} -> {} {}", linkfile.src, linkfile.dest, symbol);
                }
            }
        }
    }

    // Process manifest repo links
    if let Some(ref manifest_config) = manifest.manifest {
        let manifests_dir = manifest_paths::resolve_manifest_content_dir(workspace_root);
        let spaces_dir = manifest_paths::spaces_dir(workspace_root);

        // Check manifest copyfiles
        if let Some(ref copyfiles) = manifest_config.copyfile {
            for copyfile in copyfiles {
                total_links += 1;
                let source = match resolve_file_source(&copyfile.src, &manifests_dir, &spaces_dir) {
                    Ok(p) => p,
                    Err(e) => {
                        broken_links += 1;
                        if json {
                            let label = if copyfile.src.starts_with("gripspace:") {
                                copyfile.src.clone()
                            } else {
                                format!("manifest:{}", copyfile.src)
                            };
                            json_links.push(JsonLink {
                                link_type: "copyfile".to_string(),
                                src: label,
                                dest: copyfile.dest.clone(),
                                status: format!("broken: {}", e),
                            });
                        } else {
                            Output::warning(&format!(
                                "[copy] {} -> {} ({})",
                                copyfile.src, copyfile.dest, e
                            ));
                        }
                        continue;
                    }
                };
                let dest = workspace_root.join(&copyfile.dest);

                let label = if copyfile.src.starts_with("gripspace:") {
                    copyfile.src.clone()
                } else {
                    format!("manifest:{}", copyfile.src)
                };

                let status = if source.exists() && dest.exists() {
                    valid_links += 1;
                    "valid"
                } else if !source.exists() {
                    broken_links += 1;
                    "broken: source missing"
                } else {
                    broken_links += 1;
                    "broken: dest missing"
                };

                if json {
                    json_links.push(JsonLink {
                        link_type: "copyfile".to_string(),
                        src: label,
                        dest: copyfile.dest.clone(),
                        status: status.to_string(),
                    });
                } else {
                    let symbol = if status == "valid" {
                        "✓"
                    } else {
                        &format!("✗ ({})", status.strip_prefix("broken: ").unwrap_or(status))
                    };
                    println!("  [copy] {} -> {} {}", label, copyfile.dest, symbol);
                }
            }
        }

        // Check manifest linkfiles
        if let Some(ref linkfiles) = manifest_config.linkfile {
            for linkfile in linkfiles {
                total_links += 1;
                let source = match resolve_file_source(&linkfile.src, &manifests_dir, &spaces_dir) {
                    Ok(p) => p,
                    Err(e) => {
                        broken_links += 1;
                        if json {
                            let label = if linkfile.src.starts_with("gripspace:") {
                                linkfile.src.clone()
                            } else {
                                format!("manifest:{}", linkfile.src)
                            };
                            json_links.push(JsonLink {
                                link_type: "linkfile".to_string(),
                                src: label,
                                dest: linkfile.dest.clone(),
                                status: format!("broken: {}", e),
                            });
                        } else {
                            Output::warning(&format!(
                                "[link] {} -> {} ({})",
                                linkfile.src, linkfile.dest, e
                            ));
                        }
                        continue;
                    }
                };
                let dest = workspace_root.join(&linkfile.dest);

                let label = if linkfile.src.starts_with("gripspace:") {
                    linkfile.src.clone()
                } else {
                    format!("manifest:{}", linkfile.src)
                };

                let status = if source.exists() && dest.exists() && dest.is_symlink() {
                    valid_links += 1;
                    "valid"
                } else if !source.exists() {
                    broken_links += 1;
                    "broken: source missing"
                } else if !dest.exists() {
                    broken_links += 1;
                    "broken: link missing"
                } else {
                    broken_links += 1;
                    "broken: not a symlink"
                };

                if json {
                    json_links.push(JsonLink {
                        link_type: "linkfile".to_string(),
                        src: label,
                        dest: linkfile.dest.clone(),
                        status: status.to_string(),
                    });
                } else {
                    let symbol = if status == "valid" {
                        "✓"
                    } else {
                        &format!("✗ ({})", status.strip_prefix("broken: ").unwrap_or(status))
                    };
                    println!("  [link] {} -> {} {}", label, linkfile.dest, symbol);
                }
            }
        }

        // Show composefile status
        if let Some(ref composefiles) = manifest_config.composefile {
            for compose in composefiles {
                total_links += 1;
                let dest = workspace_root.join(&compose.dest);

                let status = if dest.exists() {
                    valid_links += 1;
                    "valid"
                } else {
                    broken_links += 1;
                    "broken: not generated"
                };

                let parts_desc: Vec<String> = compose
                    .parts
                    .iter()
                    .map(|p| {
                        if let Some(ref gs) = p.gripspace {
                            format!("{}:{}", gs, p.src)
                        } else {
                            p.src.clone()
                        }
                    })
                    .collect();

                if json {
                    json_links.push(JsonLink {
                        link_type: "composefile".to_string(),
                        src: parts_desc.join(" + "),
                        dest: compose.dest.clone(),
                        status: status.to_string(),
                    });
                } else {
                    let symbol = if status == "valid" {
                        "✓"
                    } else {
                        &format!("✗ ({})", status.strip_prefix("broken: ").unwrap_or(status))
                    };
                    println!(
                        "  [compose] [{}] -> {} {}",
                        parts_desc.join(" + "),
                        compose.dest,
                        symbol
                    );
                }
            }
        }
    }

    if json {
        #[derive(serde::Serialize)]
        struct JsonLinkStatus {
            links: Vec<JsonLink>,
            valid: usize,
            broken: usize,
        }

        let result = JsonLinkStatus {
            links: json_links,
            valid: valid_links,
            broken: broken_links,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        println!();
        if total_links == 0 {
            println!("No file links defined in manifest.");
        } else if broken_links == 0 {
            Output::success(&format!("All {} link(s) valid", valid_links));
        } else {
            Output::warning(&format!(
                "{} valid, {} broken out of {} total",
                valid_links, broken_links, total_links
            ));
            println!();
            println!("Run 'gr link --apply' to fix broken links.");
        }
    }

    Ok(())
}

pub fn apply_links(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    quiet: bool,
) -> anyhow::Result<()> {
    if !quiet {
        Output::header("Applying File Links");
        println!();
    }

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

    let mut applied = 0;
    let mut errors = 0;

    for (name, config) in &manifest.repos {
        let repo = repos.iter().find(|r| &r.name == name);

        if !repo.map(|r| path_exists(&r.absolute_path)).unwrap_or(false) {
            continue;
        }

        // Apply copyfiles
        if let Some(ref copyfiles) = config.copyfile {
            for copyfile in copyfiles {
                let source = repo
                    .map(|r| r.absolute_path.join(&copyfile.src))
                    .unwrap_or_else(|| workspace_root.join(&config.path).join(&copyfile.src));
                let dest = workspace_root.join(&copyfile.dest);

                if !source.exists() {
                    Output::warning(&format!("Source not found: {:?}", source));
                    errors += 1;
                    continue;
                }

                // Create parent directory if needed
                if let Some(parent) = dest.parent() {
                    std::fs::create_dir_all(parent)?;
                }

                match std::fs::copy(&source, &dest) {
                    Ok(_) => {
                        if !quiet {
                            Output::success(&format!(
                                "[copy] {} -> {}",
                                copyfile.src, copyfile.dest
                            ));
                        }
                        applied += 1;
                    }
                    Err(e) => {
                        Output::error(&format!("Failed to copy: {}", e));
                        errors += 1;
                    }
                }
            }
        }

        // Apply linkfiles
        if let Some(ref linkfiles) = config.linkfile {
            for linkfile in linkfiles {
                let source = repo
                    .map(|r| r.absolute_path.join(&linkfile.src))
                    .unwrap_or_else(|| workspace_root.join(&config.path).join(&linkfile.src));
                let dest = workspace_root.join(&linkfile.dest);

                if !source.exists() {
                    Output::warning(&format!("Source not found: {:?}", source));
                    errors += 1;
                    continue;
                }

                // Create parent directory if needed
                if let Some(parent) = dest.parent() {
                    std::fs::create_dir_all(parent)?;
                }

                // Remove existing link/file if present
                if dest.exists() || dest.is_symlink() {
                    let _ = std::fs::remove_file(&dest);
                }

                #[cfg(unix)]
                {
                    match std::os::unix::fs::symlink(&source, &dest) {
                        Ok(_) => {
                            if !quiet {
                                Output::success(&format!(
                                    "[link] {} -> {}",
                                    linkfile.src, linkfile.dest
                                ));
                            }
                            applied += 1;
                        }
                        Err(e) => {
                            Output::error(&format!("Failed to create symlink: {}", e));
                            errors += 1;
                        }
                    }
                }

                #[cfg(windows)]
                {
                    // On Windows, use junction for directories, symlink for files
                    if source.is_dir() {
                        match std::os::windows::fs::symlink_dir(&source, &dest) {
                            Ok(_) => {
                                if !quiet {
                                    Output::success(&format!(
                                        "[link] {} -> {}",
                                        linkfile.src, linkfile.dest
                                    ));
                                }
                                applied += 1;
                            }
                            Err(e) => {
                                Output::error(&format!("Failed to create symlink: {}", e));
                                errors += 1;
                            }
                        }
                    } else {
                        match std::os::windows::fs::symlink_file(&source, &dest) {
                            Ok(_) => {
                                if !quiet {
                                    Output::success(&format!(
                                        "[link] {} -> {}",
                                        linkfile.src, linkfile.dest
                                    ));
                                }
                                applied += 1;
                            }
                            Err(e) => {
                                Output::error(&format!("Failed to create symlink: {}", e));
                                errors += 1;
                            }
                        }
                    }
                }
            }
        }
    }

    // Apply manifest repo links
    if let Some(ref manifest_config) = manifest.manifest {
        let manifests_dir = manifest_paths::resolve_manifest_content_dir(workspace_root);
        let spaces_dir = manifest_paths::spaces_dir(workspace_root);

        if manifests_dir.exists() {
            // Apply manifest copyfiles
            if let Some(ref copyfiles) = manifest_config.copyfile {
                for copyfile in copyfiles {
                    let source =
                        match resolve_file_source(&copyfile.src, &manifests_dir, &spaces_dir) {
                            Ok(p) => p,
                            Err(e) => {
                                Output::warning(&format!("Invalid source path: {}", e));
                                errors += 1;
                                continue;
                            }
                        };
                    let dest = workspace_root.join(&copyfile.dest);

                    if !source.exists() {
                        Output::warning(&format!("Source not found: {:?}", source));
                        errors += 1;
                        continue;
                    }

                    // Create parent directory if needed
                    if let Some(parent) = dest.parent() {
                        std::fs::create_dir_all(parent)?;
                    }

                    let label = if copyfile.src.starts_with("gripspace:") {
                        copyfile.src.clone()
                    } else {
                        format!("manifest:{}", copyfile.src)
                    };

                    match std::fs::copy(&source, &dest) {
                        Ok(_) => {
                            if !quiet {
                                Output::success(&format!("[copy] {} -> {}", label, copyfile.dest));
                            }
                            applied += 1;
                        }
                        Err(e) => {
                            Output::error(&format!("Failed to copy: {}", e));
                            errors += 1;
                        }
                    }
                }
            }

            // Apply manifest linkfiles
            if let Some(ref linkfiles) = manifest_config.linkfile {
                for linkfile in linkfiles {
                    let source =
                        match resolve_file_source(&linkfile.src, &manifests_dir, &spaces_dir) {
                            Ok(p) => p,
                            Err(e) => {
                                Output::warning(&format!("Invalid source path: {}", e));
                                errors += 1;
                                continue;
                            }
                        };
                    let dest = workspace_root.join(&linkfile.dest);

                    if !source.exists() {
                        Output::warning(&format!("Source not found: {:?}", source));
                        errors += 1;
                        continue;
                    }

                    // Create parent directory if needed
                    if let Some(parent) = dest.parent() {
                        std::fs::create_dir_all(parent)?;
                    }

                    // Remove existing link/file if present
                    if dest.exists() || dest.is_symlink() {
                        let _ = std::fs::remove_file(&dest);
                    }

                    let label = if linkfile.src.starts_with("gripspace:") {
                        linkfile.src.clone()
                    } else {
                        format!("manifest:{}", linkfile.src)
                    };

                    #[cfg(unix)]
                    {
                        match std::os::unix::fs::symlink(&source, &dest) {
                            Ok(_) => {
                                if !quiet {
                                    Output::success(&format!(
                                        "[link] {} -> {}",
                                        label, linkfile.dest
                                    ));
                                }
                                applied += 1;
                            }
                            Err(e) => {
                                Output::error(&format!("Failed to create symlink: {}", e));
                                errors += 1;
                            }
                        }
                    }

                    #[cfg(windows)]
                    {
                        if source.is_dir() {
                            match std::os::windows::fs::symlink_dir(&source, &dest) {
                                Ok(_) => {
                                    if !quiet {
                                        Output::success(&format!(
                                            "[link] {} -> {}",
                                            label, linkfile.dest
                                        ));
                                    }
                                    applied += 1;
                                }
                                Err(e) => {
                                    Output::error(&format!("Failed to create symlink: {}", e));
                                    errors += 1;
                                }
                            }
                        } else {
                            match std::os::windows::fs::symlink_file(&source, &dest) {
                                Ok(_) => {
                                    if !quiet {
                                        Output::success(&format!(
                                            "[link] {} -> {}",
                                            label, linkfile.dest
                                        ));
                                    }
                                    applied += 1;
                                }
                                Err(e) => {
                                    Output::error(&format!("Failed to create symlink: {}", e));
                                    errors += 1;
                                }
                            }
                        }
                    }
                }
            }

            // Apply composefiles
            if let Some(ref composefiles) = manifest_config.composefile {
                if !composefiles.is_empty() {
                    match process_composefiles(
                        workspace_root,
                        &manifests_dir,
                        &spaces_dir,
                        composefiles,
                    ) {
                        Ok(()) => {
                            for compose in composefiles {
                                if !quiet {
                                    Output::success(&format!("[compose] -> {}", compose.dest));
                                }
                                applied += 1;
                            }
                        }
                        Err(e) => {
                            Output::error(&format!("Failed to process composefiles: {}", e));
                            errors += 1;
                        }
                    }
                }
            }
        }
    }

    if !quiet {
        println!();
    }
    if errors == 0 {
        Output::success(&format!("Applied {} link(s)", applied));
    } else {
        Output::warning(&format!("{} applied, {} errors", applied, errors));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::manifest::{
        CopyFileConfig, LinkFileConfig, ManifestRepoConfig, ManifestSettings, MergeStrategy,
        RepoConfig,
    };
    use std::collections::HashMap;
    use tempfile::TempDir;

    fn create_test_manifest(
        copyfiles: Option<Vec<CopyFileConfig>>,
        linkfiles: Option<Vec<LinkFileConfig>>,
    ) -> Manifest {
        let mut repos = HashMap::new();
        repos.insert(
            "test-repo".to_string(),
            RepoConfig {
                url: Some("git@github.com:user/test-repo.git".to_string()),
                remote: None,
                path: "test-repo".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: copyfiles,
                linkfile: linkfiles,
                platform: None,
                reference: false,
                groups: Vec::new(),
                agent: None,
            },
        );

        Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: None,
            repos,
            settings: ManifestSettings {
                pr_prefix: "[cross-repo]".to_string(),
                merge_strategy: MergeStrategy::default(),
                revision: None,
                target: None,
                sync_remote: None,
                push_remote: None,
            },
            workspace: None,
        }
    }

    #[test]
    fn test_show_link_status_no_links() {
        let temp = TempDir::new().unwrap();
        let manifest = create_test_manifest(None, None);

        // Should not error even with no links
        let result = show_link_status(&temp.path().to_path_buf(), &manifest, false);
        assert!(result.is_ok());
    }

    #[test]
    fn test_apply_copyfile() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory and source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("README.md"), "# Test").unwrap();

        let copyfiles = vec![CopyFileConfig {
            src: "README.md".to_string(),
            dest: "REPO_README.md".to_string(),
        }];

        let manifest = create_test_manifest(Some(copyfiles), None);

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify the file was copied
        let dest_path = workspace.join("REPO_README.md");
        assert!(dest_path.exists());
        let content = std::fs::read_to_string(&dest_path).unwrap();
        assert_eq!(content, "# Test");
    }

    #[test]
    #[cfg(unix)]
    fn test_apply_linkfile() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory and source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("config.yaml"), "key: value").unwrap();

        let linkfiles = vec![LinkFileConfig {
            src: "config.yaml".to_string(),
            dest: "linked-config.yaml".to_string(),
        }];

        let manifest = create_test_manifest(None, Some(linkfiles));

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify the symlink was created
        let dest_path = workspace.join("linked-config.yaml");
        assert!(dest_path.exists());
        assert!(dest_path.is_symlink());

        // Verify we can read through the symlink
        let content = std::fs::read_to_string(&dest_path).unwrap();
        assert_eq!(content, "key: value");
    }

    #[test]
    fn test_apply_links_missing_source() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory but NOT the source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();

        let copyfiles = vec![CopyFileConfig {
            src: "nonexistent.txt".to_string(),
            dest: "dest.txt".to_string(),
        }];

        let manifest = create_test_manifest(Some(copyfiles), None);

        // Should succeed but skip the missing file
        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Dest should not exist
        assert!(!workspace.join("dest.txt").exists());
    }

    #[test]
    fn test_apply_links_creates_parent_dirs() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory and source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("file.txt"), "content").unwrap();

        let copyfiles = vec![CopyFileConfig {
            src: "file.txt".to_string(),
            dest: "nested/dir/file.txt".to_string(),
        }];

        let manifest = create_test_manifest(Some(copyfiles), None);

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify nested directory was created
        let dest_path = workspace.join("nested/dir/file.txt");
        assert!(dest_path.exists());
    }

    #[test]
    fn test_copyfile_overwrites_existing() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory and source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("config.txt"), "new content").unwrap();

        // Create existing destination file
        std::fs::write(workspace.join("config.txt"), "old content").unwrap();

        let copyfiles = vec![CopyFileConfig {
            src: "config.txt".to_string(),
            dest: "config.txt".to_string(),
        }];

        let manifest = create_test_manifest(Some(copyfiles), None);

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify the file was overwritten
        let content = std::fs::read_to_string(workspace.join("config.txt")).unwrap();
        assert_eq!(content, "new content");
    }

    #[test]
    fn test_manifest_copyfile() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create manifest directory and source file
        let manifests_dir = workspace.join(".gitgrip").join("spaces").join("main");
        std::fs::create_dir_all(&manifests_dir).unwrap();
        std::fs::write(manifests_dir.join("CLAUDE.md"), "# Claude Guide").unwrap();

        // Create manifest with manifest-level copyfile
        let mut repos = std::collections::HashMap::new();
        repos.insert(
            "test-repo".to_string(),
            RepoConfig {
                url: Some("git@github.com:test/repo.git".to_string()),
                remote: None,
                path: "test-repo".to_string(),
                revision: Some("main".to_string()),
                target: None,
                sync_remote: None,
                push_remote: None,
                copyfile: None,
                linkfile: None,
                platform: None,
                reference: false,
                groups: Vec::new(),
                agent: None,
            },
        );

        let manifest = Manifest {
            version: 2,
            remotes: None,
            gripspaces: None,
            manifest: Some(ManifestRepoConfig {
                url: "git@github.com:test/manifest.git".to_string(),
                revision: Some("main".to_string()),
                copyfile: Some(vec![CopyFileConfig {
                    src: "CLAUDE.md".to_string(),
                    dest: "CLAUDE.md".to_string(),
                }]),
                linkfile: None,
                composefile: None,
                platform: None,
            }),
            repos,
            settings: ManifestSettings {
                pr_prefix: "[cross-repo]".to_string(),
                merge_strategy: MergeStrategy::default(),
                revision: None,
                target: None,
                sync_remote: None,
                push_remote: None,
            },
            workspace: None,
        };

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify the manifest file was copied to workspace root
        let dest_path = workspace.join("CLAUDE.md");
        assert!(dest_path.exists());
        let content = std::fs::read_to_string(&dest_path).unwrap();
        assert_eq!(content, "# Claude Guide");
    }

    #[test]
    #[cfg(unix)]
    fn test_linkfile_points_to_source() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory and source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("shared.config"), "shared config").unwrap();

        let linkfiles = vec![LinkFileConfig {
            src: "shared.config".to_string(),
            dest: "linked.config".to_string(),
        }];

        let manifest = create_test_manifest(None, Some(linkfiles));

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify symlink points to the source file
        let dest_path = workspace.join("linked.config");
        let link_target = std::fs::read_link(&dest_path).unwrap();
        let expected_source = repo_dir.join("shared.config");

        // The target should resolve to the source file
        assert!(
            link_target.ends_with("test-repo/shared.config"),
            "Symlink should point to source, got: {:?}",
            link_target
        );
    }

    #[test]
    #[cfg(unix)]
    fn test_linkfile_replaces_existing_file() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().to_path_buf();

        // Create repo directory and source file
        let repo_dir = workspace.join("test-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("config.yaml"), "new: value").unwrap();

        // Create existing regular file at destination
        std::fs::write(workspace.join("linked.yaml"), "old: value").unwrap();

        let linkfiles = vec![LinkFileConfig {
            src: "config.yaml".to_string(),
            dest: "linked.yaml".to_string(),
        }];

        let manifest = create_test_manifest(None, Some(linkfiles));

        let result = apply_links(&workspace, &manifest, true);
        assert!(result.is_ok());

        // Verify the destination is now a symlink
        let dest_path = workspace.join("linked.yaml");
        assert!(dest_path.is_symlink());

        // Verify content through symlink
        let content = std::fs::read_to_string(&dest_path).unwrap();
        assert_eq!(content, "new: value");
    }
}

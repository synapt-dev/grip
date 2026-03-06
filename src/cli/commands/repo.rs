//! Repo command implementation
//!
//! Manages repositories in the workspace.

use crate::cli::output::{Output, Table};
use crate::core::manifest::Manifest;
use crate::core::manifest_paths;
use crate::core::repo::RepoInfo;
use crate::git::path_exists;
use std::path::Path;

/// Run repo list command
pub fn run_repo_list(workspace_root: &Path, manifest: &Manifest) -> anyhow::Result<()> {
    Output::header("Repositories");
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

    let mut table = Table::new(vec!["Name", "Path", "Branch", "Status"]);

    for repo in &repos {
        let status = if path_exists(&repo.absolute_path) {
            "cloned"
        } else {
            "not cloned"
        };

        table.add_row(vec![&repo.name, &repo.path, &repo.revision, status]);
    }

    table.print();

    println!();
    let cloned = repos
        .iter()
        .filter(|r| path_exists(&r.absolute_path))
        .count();
    println!("{}/{} repositories cloned", cloned, repos.len());

    Ok(())
}

/// Run repo add command
pub fn run_repo_add(
    workspace_root: &Path,
    url: &str,
    path: Option<&str>,
    default_branch: Option<&str>,
    target: Option<&str>,
) -> anyhow::Result<()> {
    Output::header("Adding repository");
    println!();

    // Parse URL to get repo name
    let repo_name = extract_repo_name(url)
        .ok_or_else(|| anyhow::anyhow!("Could not parse repository name from URL"))?;

    let repo_path = path
        .map(|p| p.to_string())
        .unwrap_or_else(|| repo_name.clone());

    let branch = default_branch.unwrap_or("main").to_string();

    // Load manifest
    let manifest_path = manifest_paths::resolve_manifest_path_for_update(workspace_root)
        .ok_or_else(|| anyhow::anyhow!("No workspace manifest found to update"))?;
    let content = std::fs::read_to_string(&manifest_path)?;

    // Simple YAML append (for a proper implementation, use serde_yaml to read/write)
    let mut new_repo_yaml = format!(
        r#"
  {}:
    url: {}
    path: {}
    default_branch: {}"#,
        repo_name, url, repo_path, branch
    );
    if let Some(t) = target {
        new_repo_yaml.push_str(&format!("\n    target: {}", t));
    }

    // Check if repos section exists and append
    let updated_content = if content.contains("repos:") {
        // Find where to insert - after repos: and before next top-level key
        let mut lines: Vec<&str> = content.lines().collect();
        let mut after_repos = false;
        let mut insert_index = lines.len();

        for (i, line) in lines.iter().enumerate() {
            if line.starts_with("repos:") {
                after_repos = true;
                continue;
            }

            // If we're after repos: section and hit a new top-level key, insert here
            if after_repos
                && (line.starts_with("settings:")
                    || line.starts_with("workspace:")
                    || line.starts_with("manifest:"))
            {
                insert_index = i;
                break;
            }
        }

        lines.insert(insert_index, &new_repo_yaml);
        lines.join("\n")
    } else {
        format!("{}repos:{}", content, new_repo_yaml)
    };

    std::fs::write(&manifest_path, &updated_content)?;
    manifest_paths::sync_legacy_mirror_if_present(
        workspace_root,
        &manifest_path,
        &updated_content,
    )?;

    Output::success(&format!("Added repository '{}' to manifest", repo_name));
    println!();
    println!("Run 'gr sync' to clone the repository.");

    Ok(())
}

/// Run repo remove command
pub fn run_repo_remove(
    workspace_root: &Path,
    name: &str,
    delete_files: bool,
) -> anyhow::Result<()> {
    Output::header(&format!("Removing repository '{}'", name));
    println!();

    // Load manifest to get repo path
    let manifest_path = manifest_paths::resolve_manifest_path_for_update(workspace_root)
        .ok_or_else(|| anyhow::anyhow!("No workspace manifest found to update"))?;
    let content = std::fs::read_to_string(&manifest_path)?;
    let manifest = Manifest::parse(&content)?;

    let repo_config = manifest
        .repos
        .get(name)
        .ok_or_else(|| anyhow::anyhow!("Repository '{}' not found in manifest", name))?;

    // Delete files if requested
    if delete_files {
        let repo_path = workspace_root.join(&repo_config.path);
        if repo_path.exists() {
            let spinner = Output::spinner("Removing repository files...");
            std::fs::remove_dir_all(&repo_path)?;
            spinner.finish_with_message("Files removed");
        }
    }

    // Remove from manifest (simple string replacement)
    // For a proper implementation, use serde_yaml to read/write
    let repo_pattern = format!("  {}:", name);
    let lines: Vec<&str> = content.lines().collect();
    let mut new_lines: Vec<&str> = Vec::new();
    let mut skip_until_next_repo = false;

    for line in lines {
        if line.starts_with(&repo_pattern) {
            skip_until_next_repo = true;
            continue;
        }

        if skip_until_next_repo {
            // Check if this is a new repo entry or top-level key
            if line.starts_with("  ") && !line.starts_with("    ") && line.contains(':') {
                skip_until_next_repo = false;
            } else if !line.starts_with("  ") && !line.starts_with("    ") {
                skip_until_next_repo = false;
            } else {
                continue;
            }
        }

        if !skip_until_next_repo {
            new_lines.push(line);
        }
    }

    let updated_content = new_lines.join("\n");
    std::fs::write(&manifest_path, &updated_content)?;
    manifest_paths::sync_legacy_mirror_if_present(
        workspace_root,
        &manifest_path,
        &updated_content,
    )?;

    Output::success(&format!("Removed repository '{}' from manifest", name));
    Ok(())
}

/// Extract repository name from URL
fn extract_repo_name(url: &str) -> Option<String> {
    // Handle SSH URLs: git@github.com:owner/repo.git
    if url.starts_with("git@") {
        let parts: Vec<&str> = url.split('/').collect();
        if let Some(last) = parts.last() {
            return Some(last.trim_end_matches(".git").to_string());
        }
    }

    // Handle HTTPS URLs: https://github.com/owner/repo.git
    if url.starts_with("https://") || url.starts_with("http://") {
        let parts: Vec<&str> = url.split('/').collect();
        if let Some(last) = parts.last() {
            return Some(last.trim_end_matches(".git").to_string());
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_repo_name_ssh() {
        assert_eq!(
            extract_repo_name("git@github.com:owner/my-repo.git"),
            Some("my-repo".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_https() {
        assert_eq!(
            extract_repo_name("https://github.com/owner/my-repo.git"),
            Some("my-repo".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_no_extension() {
        assert_eq!(
            extract_repo_name("https://github.com/owner/my-repo"),
            Some("my-repo".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_gitlab() {
        assert_eq!(
            extract_repo_name("git@gitlab.com:group/subgroup/repo.git"),
            Some("repo".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_azure_devops() {
        assert_eq!(
            extract_repo_name("https://dev.azure.com/org/project/_git/my-repo"),
            Some("my-repo".to_string())
        );
    }

    #[test]
    fn test_extract_repo_name_invalid() {
        assert_eq!(extract_repo_name("not-a-url"), None);
    }

    #[test]
    fn test_extract_repo_name_nested_path() {
        // Nested paths work correctly - splits on '/' and strips .git
        assert_eq!(
            extract_repo_name("git@github.com:org/nested/repo.git"),
            Some("repo".to_string())
        );
    }
}

#[cfg(test)]
mod yaml_insertion_tests {
    use super::*;

    /// Helper function to extract the insertion logic for testing
    fn test_insert_yaml(content: &str, new_entry: &str) -> String {
        test_helper_insert(content, new_entry)
    }

    /// Replicates the insertion logic from run_repo_add for testing
    fn test_helper_insert(content: &str, new_entry: &str) -> String {
        if content.contains("repos:") {
            let mut lines: Vec<&str> = content.lines().collect();
            let mut after_repos = false;
            let mut insert_index = lines.len();

            for (i, line) in lines.iter().enumerate() {
                if line.starts_with("repos:") {
                    after_repos = true;
                    continue;
                }

                if after_repos
                    && (line.starts_with("settings:")
                        || line.starts_with("workspace:")
                        || line.starts_with("manifest:"))
                {
                    insert_index = i;
                    break;
                }
            }

            lines.insert(insert_index, new_entry);
            lines.join("\n")
        } else {
            format!("{}repos:{}", content, new_entry)
        }
    }

    fn normalize(s: &str) -> String {
        s.trim()
            .lines()
            .map(|l| l.trim_end())
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn test_insert_repo_before_settings_section() {
        let manifest = normalize(
            r#"
version: 1

manifest:
  url: https://github.com/example/workspace.git

repos:
  existing:
    url: https://github.com/example/repo.git
    path: ./repo
    default_branch: main

settings:
  merge_strategy: all-or-nothing"#,
        );

        let new_repo = "  newrepo:\n    url: https://github.com/example/new.git\n    path: ./new\n    default_branch: main";

        let result = test_insert_yaml(&manifest, new_repo);

        // Verify newrepo is AFTER repos: and BEFORE settings:
        assert!(result.contains("newrepo:"), "Result should contain newrepo");

        let repos_pos = result.find("repos:").unwrap();
        let newrepo_pos = result.find("newrepo:").unwrap();
        let settings_pos = result.find("settings:").unwrap();

        assert!(
            repos_pos < newrepo_pos && newrepo_pos < settings_pos,
            "Order should be: repos: < newrepo: < settings:"
        );
    }

    #[test]
    fn test_insert_repo_with_workspace_section() {
        let manifest = normalize(
            r#"
version: 1

manifest:
  url: https://github.com/example/workspace.git

repos:
  existing:
    url: https://github.com/example/repo.git

workspace:
  root: ."#,
        );

        let new_repo = "  newrepo:\n    url: https://github.com/example/new.git\n    path: ./new";

        let result = test_insert_yaml(&manifest, new_repo);

        let repos_pos = result.find("repos:").unwrap();
        let newrepo_pos = result.find("newrepo:").unwrap();
        let workspace_pos = result.find("workspace:").unwrap();

        assert!(
            repos_pos < newrepo_pos && newrepo_pos < workspace_pos,
            "Order should be: repos: < newrepo: < workspace:"
        );
    }

    #[test]
    fn test_insert_repo_with_manifest_after_repos() {
        // Edge case: manifest section appears AFTER repos (unusual but valid)
        let manifest = normalize(
            r#"
version: 1

repos:
  existing:
    url: https://github.com/example/repo.git

manifest:
  url: https://github.com/example/different.git"#,
        );

        let new_repo = "  newrepo:\n    url: https://github.com/example/new.git";

        let result = test_insert_yaml(&manifest, new_repo);

        let repos_pos = result.find("repos:").unwrap();
        let newrepo_pos = result.find("newrepo:").unwrap();
        let manifest_pos = result.find("manifest:").unwrap();

        assert!(
            repos_pos < newrepo_pos && newrepo_pos < manifest_pos,
            "Order should be: repos: < newrepo: < manifest:"
        );
    }

    #[test]
    fn test_insert_repo_no_section_after_repos() {
        // repos is the last section in the file
        let manifest = normalize(
            r#"
version: 1

manifest:
  url: https://github.com/example/workspace.git

repos:
  existing:
    url: https://github.com/example/repo.git"#,
        );

        let new_repo = "  newrepo:\n    url: https://github.com/example/new.git";

        let result = test_insert_yaml(&manifest, new_repo);

        let repos_pos = result.find("repos:").unwrap();
        let newrepo_pos = result.find("newrepo:").unwrap();

        // newrepo should exist and be after repos
        assert!(newrepo_pos > repos_pos, "newrepo: should be after repos:");

        // Count occurrences - should have 2 repos now
        let repo_count = result.matches("repos:").count() + result.matches("newrepo:").count();
        assert!(repo_count >= 2, "Should have at least 2 repo entries");
    }

    #[test]
    fn test_insert_repo_correct_indentation() {
        // Verify new repo entry has correct 2-space indentation
        let manifest = normalize(
            r#"
version: 1

manifest:
  url: https://github.com/example/workspace.git

repos:
  existing:
    url: https://github.com/example/repo.git
    path: ./repo
settings: {}"#,
        );

        let new_repo = "  newrepo:\n    url: https://github.com/example/new.git\n    path: ./new";

        let result = test_insert_yaml(&manifest, new_repo);

        // Find the newrepo line and verify indentation
        let newrepo_line = result
            .lines()
            .find(|l| l.trim_start().starts_with("newrepo:"))
            .expect("Should find newrepo: line");

        let leading_spaces = newrepo_line.len() - newrepo_line.trim_start().len();
        assert_eq!(
            leading_spaces, 2,
            "newrepo: should have exactly 2-space indent"
        );
    }

    #[test]
    fn test_insert_multiple_repos_sequential() {
        // Test adding multiple repos in sequence
        let manifest = normalize(
            r#"
version: 1

repos:
  repo_a:
    url: https://github.com/example/a.git
settings: {}"#,
        );

        let new_repo_b = "  repo_b:\n    url: https://github.com/example/b.git";
        let new_repo_c = "  repo_c:\n    url: https://github.com/example/c.git";

        let after_b = test_insert_yaml(&manifest, new_repo_b);
        let after_c = test_insert_yaml(&after_b, new_repo_c);

        let a_pos = after_c.find("repo_a:").unwrap();
        let b_pos = after_c.find("repo_b:").unwrap();
        let c_pos = after_c.find("repo_c:").unwrap();

        assert!(
            a_pos < b_pos && b_pos < c_pos,
            "Repos should be in order: a, b, c"
        );
    }

    #[test]
    fn test_insert_repo_with_empty_repos_section() {
        // Insert into empty repos section
        let manifest = normalize(
            r#"
version: 1

manifest:
  url: https://github.com/example/workspace.git

repos:
settings: {}"#,
        );

        let new_repo = "  newrepo:\n    url: https://github.com/example/new.git\n    path: ./new";

        let result = test_insert_yaml(&manifest, new_repo);

        // Verify newrepo is between repos: and settings:
        let repos_pos = result.find("repos:").unwrap();
        let newrepo_pos = result.find("newrepo:").unwrap();
        let settings_pos = result.find("settings:").unwrap();

        assert!(
            repos_pos < newrepo_pos && newrepo_pos < settings_pos,
            "newrepo should be between repos: and settings:"
        );
    }

    #[test]
    fn test_insert_repo_does_not_corrupt_manifest() {
        // Ensure we don't break the overall structure
        let manifest = normalize(
            r#"
version: 1

manifest:
  url: https://github.com/example/workspace.git
  linkfile:
    - src: CLAUDE.md
      dest: CLAUDE.md

repos:
  existing:
    url: https://github.com/example/repo.git
    path: ./repo
    default_branch: main
    linkfile:
      - src: .claude/skills/gitgrip
        dest: .claude/skills/gitgrip

settings:
  pr_prefix: "[cross-repo]"
  merge_strategy: all-or-nothing"#,
        );

        let new_repo = "  newtool:\n    url: https://github.com/example/newtool.git\n    path: ./newtool\n    default_branch: main";

        let result = test_insert_yaml(&manifest, new_repo);

        // Verify structure is preserved
        assert!(
            result.starts_with("version: 1"),
            "Should start with version"
        );
        assert!(
            result.contains("manifest:") && result.contains("linkfile:"),
            "Should preserve manifest section"
        );
        assert!(result.contains("repos:"), "Should contain repos section");
        assert!(
            result.contains("settings:") && result.contains("merge_strategy:"),
            "Should preserve settings section"
        );
    }
}

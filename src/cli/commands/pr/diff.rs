//! PR diff command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::RepoInfo;
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;
use std::path::PathBuf;

/// Run the PR diff command
pub async fn run_pr_diff(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    stat_only: bool,
) -> anyhow::Result<()> {
    if !stat_only {
        Output::header("PR Diff");
        println!();
    }

    let repos: Vec<RepoInfo> = manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(name, config, workspace_root, &manifest.settings)
        })
        .collect();

    let mut total_files = 0;
    let mut total_additions = 0;
    let mut total_deletions = 0;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let git_repo = match open_repo(&repo.absolute_path) {
            Ok(r) => r,
            Err(_) => continue,
        };

        let branch = match get_current_branch(&git_repo) {
            Ok(b) => b,
            Err(_) => continue,
        };

        // Skip if on target branch
        if branch == repo.target_branch() {
            continue;
        }

        let platform = get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

        match platform
            .find_pr_by_branch(&repo.owner, &repo.repo, &branch)
            .await
        {
            Ok(Some(pr)) => {
                match platform
                    .get_pull_request_diff(&repo.owner, &repo.repo, pr.number)
                    .await
                {
                    Ok(diff) => {
                        if stat_only {
                            // Parse diff for stats
                            let stats = parse_diff_stats(&diff);
                            println!("{} #{}:", repo.name, pr.number);
                            println!(
                                "  {} file(s), +{}, -{}",
                                stats.files, stats.additions, stats.deletions
                            );
                            total_files += stats.files;
                            total_additions += stats.additions;
                            total_deletions += stats.deletions;
                        } else {
                            println!("=== {} #{} ===", repo.name, pr.number);
                            println!("{}", diff);
                            println!();
                        }
                    }
                    Err(e) => {
                        Output::error(&format!("{}: {}", repo.name, e));
                    }
                }
            }
            Ok(None) => {
                Output::info(&format!("{}: no open PR for this branch", repo.name));
            }
            Err(e) => {
                Output::error(&format!("{}: {}", repo.name, e));
            }
        }
    }

    if stat_only && (total_files > 0 || total_additions > 0 || total_deletions > 0) {
        println!();
        println!("Total:");
        println!(
            "  {} file(s), +{}, -{}",
            total_files, total_additions, total_deletions
        );
    }

    Ok(())
}

struct DiffStats {
    files: usize,
    additions: usize,
    deletions: usize,
}

fn parse_diff_stats(diff: &str) -> DiffStats {
    let mut files = 0;
    let mut additions = 0;
    let mut deletions = 0;

    for line in diff.lines() {
        if line.starts_with("diff --git") || line.starts_with("Index:") {
            files += 1;
        } else if line.starts_with('+') && !line.starts_with("+++") {
            additions += 1;
        } else if line.starts_with('-') && !line.starts_with("---") {
            deletions += 1;
        }
    }

    DiffStats {
        files,
        additions,
        deletions,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_diff_stats() {
        let diff = r#"diff --git a/file1.rs b/file1.rs
--- a/file1.rs
+++ b/file1.rs
@@ -1,3 +1,4 @@
 line1
-old line
+new line
+added line
diff --git a/file2.rs b/file2.rs
--- a/file2.rs
+++ b/file2.rs
@@ -1,2 +1,2 @@
-removed
+added
"#;
        let stats = parse_diff_stats(diff);
        assert_eq!(stats.files, 2);
        assert_eq!(stats.additions, 3);
        assert_eq!(stats.deletions, 2);
    }
}

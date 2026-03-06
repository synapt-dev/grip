//! GC command implementation
//!
//! Runs `git gc` across all repositories with size reporting.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::filter_repos;
use crate::git::gc::{format_bytes, git_dir_size, run_git_gc};
use crate::git::path_exists;
use std::path::Path;

/// Run the gc command
pub fn run_gc(
    workspace_root: &Path,
    manifest: &Manifest,
    aggressive: bool,
    dry_run: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let repos = filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    if dry_run {
        Output::header("Repository .git sizes (dry run)...");
    } else if aggressive {
        Output::header("Running aggressive garbage collection...");
    } else {
        Output::header("Running garbage collection...");
    }
    println!();

    let mut total_before: u64 = 0;
    let mut total_after: u64 = 0;
    let mut gc_count = 0;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            Output::warning(&format!("{}: not cloned, skipping", repo.name));
            continue;
        }

        if dry_run {
            let size = git_dir_size(&repo.absolute_path);
            total_before += size;
            total_after += size;
            println!(
                "  {}: {}",
                Output::repo_name(&repo.name),
                format_bytes(size)
            );
            continue;
        }

        let spinner = Output::spinner(&format!("Running gc in {}...", repo.name));

        match run_git_gc(&repo.absolute_path, aggressive) {
            Ok(result) => {
                total_before += result.size_before;
                total_after += result.size_after;

                if result.success {
                    gc_count += 1;
                    let saved = result.size_before.saturating_sub(result.size_after);
                    if saved > 0 {
                        spinner.finish_with_message(format!(
                            "{}: {} -> {} (saved {})",
                            repo.name,
                            format_bytes(result.size_before),
                            format_bytes(result.size_after),
                            format_bytes(saved),
                        ));
                    } else {
                        spinner.finish_with_message(format!(
                            "{}: {} (no change)",
                            repo.name,
                            format_bytes(result.size_after),
                        ));
                    }
                } else {
                    spinner.finish_with_message(format!("{}: gc failed", repo.name));
                }
            }
            Err(e) => {
                spinner.finish_with_message(format!("{}: error - {}", repo.name, e));
            }
        }
    }

    println!();
    if dry_run {
        Output::info(&format!("Total .git size: {}", format_bytes(total_before)));
    } else {
        let saved = total_before.saturating_sub(total_after);
        Output::success(&format!(
            "GC complete: {} repo(s), {} -> {} (saved {})",
            gc_count,
            format_bytes(total_before),
            format_bytes(total_after),
            format_bytes(saved),
        ));
    }

    Ok(())
}

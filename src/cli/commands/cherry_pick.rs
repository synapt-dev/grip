//! Cherry-pick command implementation
//!
//! Cherry-picks a commit across repos that contain it.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::filter_repos;
use crate::git::cherry_pick::{
    cherry_pick, cherry_pick_abort, cherry_pick_continue, cherry_pick_in_progress, CherryPickResult,
};
use crate::git::path_exists;
use std::path::Path;

/// Run the cherry-pick command
pub fn run_cherry_pick(
    workspace_root: &Path,
    manifest: &Manifest,
    commit: Option<&str>,
    abort: bool,
    continue_pick: bool,
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let repos = filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    if abort {
        Output::header("Aborting cherry-pick...");
        println!();

        let mut aborted = 0;
        for repo in &repos {
            if !path_exists(&repo.absolute_path) {
                continue;
            }

            if cherry_pick_in_progress(&repo.absolute_path) {
                match cherry_pick_abort(&repo.absolute_path) {
                    Ok(()) => {
                        Output::success(&format!("{}: cherry-pick aborted", repo.name));
                        aborted += 1;
                    }
                    Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
                }
            }
        }

        if aborted == 0 {
            Output::info("No cherry-pick in progress in any repo.");
        }
        return Ok(());
    }

    if continue_pick {
        Output::header("Continuing cherry-pick...");
        println!();

        let mut continued = 0;
        for repo in &repos {
            if !path_exists(&repo.absolute_path) {
                continue;
            }

            if cherry_pick_in_progress(&repo.absolute_path) {
                match cherry_pick_continue(&repo.absolute_path) {
                    Ok(()) => {
                        Output::success(&format!("{}: cherry-pick continued", repo.name));
                        continued += 1;
                    }
                    Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
                }
            }
        }

        if continued == 0 {
            Output::info("No cherry-pick in progress in any repo.");
        }
        return Ok(());
    }

    let commit_sha = match commit {
        Some(sha) => sha,
        None => {
            anyhow::bail!("Commit SHA required. Usage: gr cherry-pick <sha>");
        }
    };

    Output::header(&format!("Cherry-picking {}...", commit_sha));
    println!();

    let mut applied = 0;
    let mut skipped = 0;
    let mut conflicts = 0;

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            Output::warning(&format!("{}: not cloned, skipping", repo.name));
            continue;
        }

        match cherry_pick(&repo.absolute_path, commit_sha) {
            CherryPickResult::Applied => {
                Output::success(&format!("{}: applied", repo.name));
                applied += 1;
            }
            CherryPickResult::CommitNotFound => {
                skipped += 1;
                // Silently skip — commit only exists in one repo typically
            }
            CherryPickResult::Conflict(msg) => {
                Output::warning(&format!("{}: conflict", repo.name));
                if !msg.is_empty() {
                    // Print first few lines of conflict info
                    for line in msg.lines().take(5) {
                        println!("    {}", line);
                    }
                }
                conflicts += 1;
            }
            CherryPickResult::Error(msg) => {
                Output::error(&format!("{}: {}", repo.name, msg.trim()));
            }
        }
    }

    println!();
    if applied > 0 {
        Output::success(&format!(
            "Cherry-picked into {} repo(s), {} skipped.",
            applied, skipped
        ));
    } else if conflicts > 0 {
        Output::warning("Resolve conflicts and run 'gr cherry-pick --continue'");
    } else if repos.is_empty() {
        Output::info("No repos matched the given filters.");
    } else {
        Output::info(&format!("Commit {} not found in any repo.", commit_sha));
    }

    Ok(())
}

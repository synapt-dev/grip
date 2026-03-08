//! Grep command implementation
//!
//! Runs `git grep` across all repositories, aggregating results
//! with repo-name prefixes for easy identification.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::path_exists;
use crate::util::log_cmd;
use std::path::Path;
use std::process::Command;

/// Run the grep command
pub fn run_grep(
    workspace_root: &Path,
    manifest: &Manifest,
    pattern: &str,
    ignore_case: bool,
    parallel: bool,
    pathspec: &[String],
    repos_filter: Option<&[String]>,
    group_filter: Option<&[String]>,
) -> anyhow::Result<()> {
    let repos: Vec<RepoInfo> =
        filter_repos(manifest, workspace_root, repos_filter, group_filter, false);

    if parallel {
        run_grep_parallel(&repos, pattern, ignore_case, pathspec)?;
    } else {
        run_grep_sequential(&repos, pattern, ignore_case, pathspec)?;
    }

    Ok(())
}

fn build_grep_args<'a>(
    pattern: &'a str,
    ignore_case: bool,
    pathspec: &'a [String],
) -> Vec<&'a str> {
    let mut args = vec!["grep", "-n"];
    if ignore_case {
        args.push("-i");
    }
    args.push(pattern);
    if !pathspec.is_empty() {
        args.push("--");
        for p in pathspec {
            args.push(p.as_str());
        }
    }
    args
}

fn run_grep_in_repo(
    repo: &RepoInfo,
    pattern: &str,
    ignore_case: bool,
    pathspec: &[String],
) -> Option<String> {
    if !path_exists(&repo.absolute_path) {
        return None;
    }

    let args = build_grep_args(pattern, ignore_case, pathspec);

    let mut cmd = Command::new("git");
    cmd.args(&args).current_dir(&repo.absolute_path);
    log_cmd(&cmd);
    let output = cmd.output().ok()?;

    if !output.status.success() {
        // git grep returns exit code 1 for no matches — not an error
        return None;
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    if stdout.is_empty() {
        return None;
    }

    // Prefix each line with repo name
    let prefixed: String = stdout
        .lines()
        .map(|line| format!("{}:{}", repo.name, line))
        .collect::<Vec<_>>()
        .join("\n");

    Some(prefixed)
}

fn run_grep_sequential(
    repos: &[RepoInfo],
    pattern: &str,
    ignore_case: bool,
    pathspec: &[String],
) -> anyhow::Result<()> {
    let mut total_matches = 0;

    for repo in repos {
        if let Some(output) = run_grep_in_repo(repo, pattern, ignore_case, pathspec) {
            let match_count = output.lines().count();
            total_matches += match_count;
            println!("{}", output);
        }
    }

    if total_matches == 0 {
        Output::info("No matches found.");
    } else {
        eprintln!();
        Output::info(&format!("{} match(es) found.", total_matches));
    }

    Ok(())
}

fn run_grep_parallel(
    repos: &[RepoInfo],
    pattern: &str,
    ignore_case: bool,
    pathspec: &[String],
) -> anyhow::Result<()> {
    use std::sync::{Arc, Mutex};
    use std::thread;

    let results = Arc::new(Mutex::new(Vec::new()));
    let mut handles = vec![];

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let repo = repo.clone();
        let pattern = pattern.to_string();
        let pathspec: Vec<String> = pathspec.to_vec();
        let results = Arc::clone(&results);

        let handle = thread::spawn(move || {
            if let Some(output) = run_grep_in_repo(&repo, &pattern, ignore_case, &pathspec) {
                let mut results = results.lock().expect("mutex poisoned");
                results.push(output);
            }
        });

        handles.push(handle);
    }

    for handle in handles {
        handle
            .join()
            .map_err(|_| anyhow::anyhow!("Worker thread panicked"))?;
    }

    let results = results.lock().expect("mutex poisoned");
    let mut total_matches = 0;

    for output in results.iter() {
        total_matches += output.lines().count();
        println!("{}", output);
    }

    if total_matches == 0 {
        Output::info("No matches found.");
    } else {
        eprintln!();
        Output::info(&format!("{} match(es) found.", total_matches));
    }

    Ok(())
}

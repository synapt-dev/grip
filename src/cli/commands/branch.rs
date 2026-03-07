//! Branch command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::git::{
    branch::{branch_exists, create_and_checkout_branch, delete_local_branch, list_local_branches},
    get_current_branch, open_repo,
};
use std::path::PathBuf;

/// Options for the branch command
pub struct BranchOptions<'a> {
    pub workspace_root: &'a PathBuf,
    pub manifest: &'a Manifest,
    pub name: Option<&'a str>,
    pub delete: bool,
    pub move_commits: bool,
    pub repos_filter: Option<&'a [String]>,
    pub group_filter: Option<&'a [String]>,
    pub json: bool,
}

/// Run the branch command
pub fn run_branch(opts: BranchOptions<'_>) -> anyhow::Result<()> {
    let mut repos: Vec<RepoInfo> = filter_repos(
        opts.manifest,
        opts.workspace_root,
        opts.repos_filter,
        opts.group_filter,
        false,
    );

    // Include manifest repo in branch operations (unless filtered out by --repo)
    if let Some(manifest_repo) = get_manifest_repo_info(opts.manifest, opts.workspace_root) {
        let include_manifest = match opts.repos_filter {
            None => true,
            Some(filter) => filter.iter().any(|r| r == "manifest"),
        };
        if include_manifest {
            repos.push(manifest_repo);
        }
    }

    match opts.name {
        Some(branch_name) if opts.delete => {
            // Delete branch
            #[derive(serde::Serialize)]
            struct JsonDeleteResult {
                repo: String,
                branch: String,
                action: String,
                #[serde(skip_serializing_if = "Option::is_none")]
                error: Option<String>,
            }

            let mut json_results: Vec<JsonDeleteResult> = Vec::new();

            if !opts.json {
                Output::header(&format!("Deleting branch '{}'", branch_name));
                println!();
            }

            for repo in &repos {
                if !repo.exists() {
                    if opts.json {
                        json_results.push(JsonDeleteResult {
                            repo: repo.name.clone(),
                            branch: branch_name.to_string(),
                            action: "skipped".to_string(),
                            error: Some("not cloned".to_string()),
                        });
                    } else {
                        Output::warning(&format!("{}: not cloned", repo.name));
                    }
                    continue;
                }

                match open_repo(&repo.absolute_path) {
                    Ok(git_repo) => {
                        if !branch_exists(&git_repo, branch_name) {
                            if opts.json {
                                json_results.push(JsonDeleteResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "not_found".to_string(),
                                    error: None,
                                });
                            } else {
                                Output::info(&format!("{}: branch doesn't exist", repo.name));
                            }
                            continue;
                        }

                        match delete_local_branch(&git_repo, branch_name, false) {
                            Ok(()) => {
                                if opts.json {
                                    json_results.push(JsonDeleteResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "deleted".to_string(),
                                        error: None,
                                    });
                                } else {
                                    Output::success(&format!("{}: deleted", repo.name));
                                }
                            }
                            Err(e) => {
                                if opts.json {
                                    json_results.push(JsonDeleteResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "error".to_string(),
                                        error: Some(e.to_string()),
                                    });
                                } else {
                                    Output::error(&format!("{}: {}", repo.name, e));
                                }
                            }
                        }
                    }
                    Err(e) => {
                        if opts.json {
                            json_results.push(JsonDeleteResult {
                                repo: repo.name.clone(),
                                branch: branch_name.to_string(),
                                action: "error".to_string(),
                                error: Some(e.to_string()),
                            });
                        } else {
                            Output::error(&format!("{}: {}", repo.name, e));
                        }
                    }
                }
            }

            if opts.json {
                println!("{}", serde_json::to_string_pretty(&json_results)?);
            }
        }
        Some(branch_name) if opts.move_commits => {
            // Move commits to new branch (create branch, reset current to remote, checkout new)
            #[derive(serde::Serialize)]
            struct JsonMoveResult {
                repo: String,
                branch: String,
                action: String,
                #[serde(skip_serializing_if = "Option::is_none")]
                from_branch: Option<String>,
                #[serde(skip_serializing_if = "Option::is_none")]
                error: Option<String>,
            }

            let mut json_results: Vec<JsonMoveResult> = Vec::new();

            if !opts.json {
                Output::header(&format!(
                    "Moving commits to branch '{}' in {} repos...",
                    branch_name,
                    repos.len()
                ));
                println!();
            }

            for repo in &repos {
                if !repo.exists() {
                    if opts.json {
                        json_results.push(JsonMoveResult {
                            repo: repo.name.clone(),
                            branch: branch_name.to_string(),
                            action: "skipped".to_string(),
                            from_branch: None,
                            error: Some("not cloned".to_string()),
                        });
                    } else {
                        Output::warning(&format!("{}: not cloned", repo.name));
                    }
                    continue;
                }

                match open_repo(&repo.absolute_path) {
                    Ok(git_repo) => {
                        let current = match get_current_branch(&git_repo) {
                            Ok(b) => b,
                            Err(e) => {
                                if opts.json {
                                    json_results.push(JsonMoveResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "error".to_string(),
                                        from_branch: None,
                                        error: Some(format!(
                                            "failed to get current branch - {}",
                                            e
                                        )),
                                    });
                                } else {
                                    Output::error(&format!(
                                        "{}: failed to get current branch - {}",
                                        repo.name, e
                                    ));
                                }
                                continue;
                            }
                        };

                        if branch_exists(&git_repo, branch_name) {
                            if opts.json {
                                json_results.push(JsonMoveResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "error".to_string(),
                                    from_branch: Some(current),
                                    error: Some(format!("branch '{}' already exists", branch_name)),
                                });
                            } else {
                                Output::error(&format!(
                                    "{}: branch '{}' already exists",
                                    repo.name, branch_name
                                ));
                            }
                            continue;
                        }

                        // Create branch at current HEAD
                        let head = match git_repo.head() {
                            Ok(h) => h,
                            Err(e) => {
                                if opts.json {
                                    json_results.push(JsonMoveResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "error".to_string(),
                                        from_branch: Some(current),
                                        error: Some(format!("failed to get HEAD - {}", e)),
                                    });
                                } else {
                                    Output::error(&format!(
                                        "{}: failed to get HEAD - {}",
                                        repo.name, e
                                    ));
                                }
                                continue;
                            }
                        };
                        let head_commit = match head.peel_to_commit() {
                            Ok(c) => c,
                            Err(e) => {
                                if opts.json {
                                    json_results.push(JsonMoveResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "error".to_string(),
                                        from_branch: Some(current),
                                        error: Some(format!("failed to get HEAD commit - {}", e)),
                                    });
                                } else {
                                    Output::error(&format!(
                                        "{}: failed to get HEAD commit - {}",
                                        repo.name, e
                                    ));
                                }
                                continue;
                            }
                        };

                        if let Err(e) = git_repo.branch(branch_name, &head_commit, false) {
                            if opts.json {
                                json_results.push(JsonMoveResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "error".to_string(),
                                    from_branch: Some(current),
                                    error: Some(format!("failed to create branch - {}", e)),
                                });
                            } else {
                                Output::error(&format!(
                                    "{}: failed to create branch - {}",
                                    repo.name, e
                                ));
                            }
                            continue;
                        }

                        // Reset current branch to origin/<current>
                        let remote_ref = format!("refs/remotes/origin/{}", current);
                        let remote_commit = match git_repo.revparse_single(&remote_ref) {
                            Ok(obj) => match obj.peel_to_commit() {
                                Ok(c) => c,
                                Err(e) => {
                                    if opts.json {
                                        json_results.push(JsonMoveResult {
                                            repo: repo.name.clone(),
                                            branch: branch_name.to_string(),
                                            action: "error".to_string(),
                                            from_branch: Some(current),
                                            error: Some(format!(
                                                "failed to find remote commit - {}",
                                                e
                                            )),
                                        });
                                    } else {
                                        Output::error(&format!(
                                            "{}: failed to find remote commit - {}",
                                            repo.name, e
                                        ));
                                    }
                                    continue;
                                }
                            },
                            Err(e) => {
                                if opts.json {
                                    json_results.push(JsonMoveResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "error".to_string(),
                                        from_branch: Some(current.clone()),
                                        error: Some(format!(
                                            "no remote tracking branch origin/{} - {}",
                                            current, e
                                        )),
                                    });
                                } else {
                                    Output::error(&format!(
                                        "{}: no remote tracking branch origin/{} - {}",
                                        repo.name, current, e
                                    ));
                                }
                                continue;
                            }
                        };

                        if let Err(e) =
                            git_repo.reset(remote_commit.as_object(), git2::ResetType::Hard, None)
                        {
                            if opts.json {
                                json_results.push(JsonMoveResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "error".to_string(),
                                    from_branch: Some(current.clone()),
                                    error: Some(format!(
                                        "failed to reset to origin/{} - {}",
                                        current, e
                                    )),
                                });
                            } else {
                                Output::error(&format!(
                                    "{}: failed to reset to origin/{} - {}",
                                    repo.name, current, e
                                ));
                            }
                            continue;
                        }

                        // Checkout the new branch
                        if let Err(e) = git_repo.set_head(&format!("refs/heads/{}", branch_name)) {
                            if opts.json {
                                json_results.push(JsonMoveResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "error".to_string(),
                                    from_branch: Some(current),
                                    error: Some(format!("failed to checkout new branch - {}", e)),
                                });
                            } else {
                                Output::error(&format!(
                                    "{}: failed to checkout new branch - {}",
                                    repo.name, e
                                ));
                            }
                            continue;
                        }

                        if let Err(e) = git_repo
                            .checkout_head(Some(git2::build::CheckoutBuilder::new().force()))
                        {
                            if opts.json {
                                json_results.push(JsonMoveResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "error".to_string(),
                                    from_branch: Some(current),
                                    error: Some(format!("failed to update working tree - {}", e)),
                                });
                            } else {
                                Output::error(&format!(
                                    "{}: failed to update working tree - {}",
                                    repo.name, e
                                ));
                            }
                            continue;
                        }

                        if opts.json {
                            json_results.push(JsonMoveResult {
                                repo: repo.name.clone(),
                                branch: branch_name.to_string(),
                                action: "moved".to_string(),
                                from_branch: Some(current.clone()),
                                error: None,
                            });
                        } else {
                            Output::success(&format!(
                                "{}: moved commits from {} to {}",
                                repo.name, current, branch_name
                            ));
                        }
                    }
                    Err(e) => {
                        if opts.json {
                            json_results.push(JsonMoveResult {
                                repo: repo.name.clone(),
                                branch: branch_name.to_string(),
                                action: "error".to_string(),
                                from_branch: None,
                                error: Some(e.to_string()),
                            });
                        } else {
                            Output::error(&format!("{}: {}", repo.name, e));
                        }
                    }
                }
            }

            if opts.json {
                println!("{}", serde_json::to_string_pretty(&json_results)?);
            } else {
                println!();
                println!(
                    "Commits moved to branch: {}",
                    Output::branch_name(branch_name)
                );
            }
        }
        Some(branch_name) => {
            // Create branch
            #[derive(serde::Serialize)]
            struct JsonCreateResult {
                repo: String,
                branch: String,
                action: String,
                #[serde(skip_serializing_if = "Option::is_none")]
                error: Option<String>,
            }

            let mut json_results: Vec<JsonCreateResult> = Vec::new();

            if !opts.json {
                Output::header(&format!(
                    "Creating branch '{}' in {} repos...",
                    branch_name,
                    repos.len()
                ));
                println!();
            }

            for repo in &repos {
                if !repo.exists() {
                    if opts.json {
                        json_results.push(JsonCreateResult {
                            repo: repo.name.clone(),
                            branch: branch_name.to_string(),
                            action: "skipped".to_string(),
                            error: Some("not cloned".to_string()),
                        });
                    } else {
                        Output::warning(&format!("{}: not cloned", repo.name));
                    }
                    continue;
                }

                match open_repo(&repo.absolute_path) {
                    Ok(git_repo) => {
                        if branch_exists(&git_repo, branch_name) {
                            if opts.json {
                                json_results.push(JsonCreateResult {
                                    repo: repo.name.clone(),
                                    branch: branch_name.to_string(),
                                    action: "already_exists".to_string(),
                                    error: None,
                                });
                            } else {
                                Output::info(&format!("{}: already exists", repo.name));
                            }
                            continue;
                        }

                        match create_and_checkout_branch(&git_repo, branch_name) {
                            Ok(()) => {
                                if opts.json {
                                    json_results.push(JsonCreateResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "created".to_string(),
                                        error: None,
                                    });
                                } else {
                                    Output::success(&format!("{}: created", repo.name));
                                }
                            }
                            Err(e) => {
                                if opts.json {
                                    json_results.push(JsonCreateResult {
                                        repo: repo.name.clone(),
                                        branch: branch_name.to_string(),
                                        action: "error".to_string(),
                                        error: Some(e.to_string()),
                                    });
                                } else {
                                    Output::error(&format!("{}: {}", repo.name, e));
                                }
                            }
                        }
                    }
                    Err(e) => {
                        if opts.json {
                            json_results.push(JsonCreateResult {
                                repo: repo.name.clone(),
                                branch: branch_name.to_string(),
                                action: "error".to_string(),
                                error: Some(e.to_string()),
                            });
                        } else {
                            Output::error(&format!("{}: {}", repo.name, e));
                        }
                    }
                }
            }

            if opts.json {
                println!("{}", serde_json::to_string_pretty(&json_results)?);
            } else {
                println!();
                println!(
                    "All repos now on branch: {}",
                    Output::branch_name(branch_name)
                );
            }
        }
        None => {
            if opts.json {
                // JSON output for list mode
                #[derive(serde::Serialize)]
                struct JsonBranch {
                    repo: String,
                    branch: String,
                    default_branch: String,
                }

                let mut results: Vec<JsonBranch> = Vec::new();
                for repo in &repos {
                    if !repo.exists() {
                        continue;
                    }
                    if let Ok(git_repo) = open_repo(&repo.absolute_path) {
                        let current = get_current_branch(&git_repo).unwrap_or_default();
                        results.push(JsonBranch {
                            repo: repo.name.clone(),
                            branch: current,
                            default_branch: repo.revision.clone(),
                        });
                    }
                }
                println!("{}", serde_json::to_string_pretty(&results)?);
            } else {
                // List branches
                Output::header("Branches");
                println!();

                for repo in &repos {
                    if !repo.exists() {
                        continue;
                    }

                    match open_repo(&repo.absolute_path) {
                        Ok(git_repo) => {
                            let current = get_current_branch(&git_repo).unwrap_or_default();
                            let branches = list_local_branches(&git_repo).unwrap_or_default();

                            println!("  {}:", Output::repo_name(&repo.name));
                            for branch in branches {
                                let marker = if branch == current { "* " } else { "  " };
                                let formatted = if branch == current {
                                    Output::branch_name(&branch)
                                } else {
                                    branch
                                };
                                println!("    {}{}", marker, formatted);
                            }
                        }
                        Err(_) => continue,
                    }
                }
            }
        }
    }

    Ok(())
}

//! Sync command implementation

use crate::cli::commands::link::apply_links;
use crate::cli::output::Output;
use crate::core::gripspace::{
    ensure_gripspace, gripspace_name, resolve_all_gripspaces, update_gripspace,
};
use crate::core::griptree::GriptreeConfig;
use crate::core::manifest::{HookCondition, Manifest};
use crate::core::manifest_paths;
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::core::sync_state::SyncSnapshot;
use crate::files::process_composefiles;
use crate::git::branch::{checkout_branch_at_upstream, checkout_detached, has_commits_ahead};
use crate::git::remote::{
    fetch_remote, pull_latest_from_upstream, reset_hard, safe_pull_latest, set_branch_upstream_ref,
};
use crate::git::status::has_uncommitted_changes;
use crate::git::{clone_repo, get_current_branch, open_repo, path_exists};
use git2::Repository;
use indicatif::ProgressBar;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use tokio::task::JoinSet;

/// Result of syncing a single repo
#[derive(Debug)]
struct SyncResult {
    name: String,
    success: bool,
    message: String,
    was_cloned: bool,
    had_changes: bool,
}

/// JSON-serializable sync result for --json output
#[derive(serde::Serialize)]
struct JsonSyncRepo {
    name: String,
    action: String,
    error: Option<String>,
}

/// Result of running a single post-sync hook
#[derive(serde::Serialize, Clone)]
struct HookResult {
    name: String,
    success: bool,
    skipped: bool,
    duration_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

/// Run the sync command
#[allow(clippy::too_many_arguments)]
pub async fn run_sync(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    force: bool,
    quiet: bool,
    group_filter: Option<&[String]>,
    sequential: bool,
    reset_refs: bool,
    json: bool,
    no_hooks: bool,
) -> anyhow::Result<()> {
    // Re-load and resolve gripspaces before syncing repos
    let manifest = sync_gripspaces(workspace_root, manifest, quiet)?;
    let manifest = &manifest;

    let mut repos: Vec<RepoInfo> = filter_repos(manifest, workspace_root, None, group_filter, true);

    // Include manifest repo at the beginning (sync it first)
    if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
        repos.insert(0, manifest_repo);
    }
    let griptree_config = GriptreeConfig::load_from_workspace(workspace_root)?;
    let griptree_branch = griptree_config.as_ref().map(|cfg| cfg.branch.clone());

    // Capture pre-sync snapshot for rollback support
    if let Ok(snapshot) = SyncSnapshot::capture(workspace_root, &repos) {
        if let Err(e) = snapshot.save(workspace_root) {
            if !quiet && !json {
                Output::warning(&format!("Could not save sync snapshot: {}", e));
            }
        }
    }

    if !json {
        Output::header(&format!("Syncing {} repositories...", repos.len()));
        println!();
    }

    let results = if sequential {
        sync_sequential(
            &repos,
            force,
            quiet,
            griptree_config.as_ref(),
            griptree_branch.as_deref(),
            reset_refs,
        )?
    } else {
        sync_parallel(
            &repos,
            force,
            quiet,
            griptree_config.clone(),
            griptree_branch.clone(),
            reset_refs,
        )
        .await?
    };

    // Display results
    let mut success_count = 0;
    let mut error_count = 0;
    let mut failed_repos: Vec<(String, String)> = Vec::new();

    // Build JSON data alongside display
    let mut json_repos: Vec<JsonSyncRepo> = Vec::new();

    for result in &results {
        let action = if result.was_cloned {
            "cloned"
        } else if result.success {
            "pulled"
        } else {
            "failed"
        };

        json_repos.push(JsonSyncRepo {
            name: result.name.clone(),
            action: action.to_string(),
            error: if !result.success {
                Some(result.message.clone())
            } else {
                None
            },
        });

        if result.success {
            success_count += 1;
        } else {
            error_count += 1;
            failed_repos.push((result.name.clone(), result.message.clone()));
        }
    }

    if !json {
        println!();
        if error_count == 0 {
            Output::success(&format!(
                "All {} repositories synced successfully.",
                success_count
            ));
        } else {
            Output::warning(&format!("{} synced, {} failed", success_count, error_count));

            if !failed_repos.is_empty() {
                println!();
                for (repo_name, error_msg) in &failed_repos {
                    println!("  ✗ {}: {}", repo_name, error_msg);
                }
            }
        }
    }

    // Process composefiles after sync
    let mut composefiles_count = 0;
    if let Some(ref manifest_config) = manifest.manifest {
        if let Some(ref composefiles) = manifest_config.composefile {
            if !composefiles.is_empty() {
                composefiles_count = composefiles.len();
                let manifests_dir = manifest_paths::resolve_manifest_content_dir(workspace_root);
                let spaces_dir = manifest_paths::spaces_dir(workspace_root);

                match process_composefiles(
                    workspace_root,
                    &manifests_dir,
                    &spaces_dir,
                    composefiles,
                ) {
                    Ok(()) => {
                        if !quiet && !json {
                            Output::success(&format!(
                                "Processed {} composefile(s)",
                                composefiles.len()
                            ));
                        }
                    }
                    Err(e) => {
                        if !json {
                            Output::warning(&format!("Composefile processing failed: {}", e));
                        }
                    }
                }
            }
        }
    }

    // Generate agent context files (after composefiles, before linkfiles)
    if let Some(agent_config) = manifest.workspace.as_ref().and_then(|w| w.agent.as_ref()) {
        if agent_config.targets.is_some() {
            match crate::cli::commands::agent::run_agent_generate_context(
                workspace_root,
                manifest,
                false,
                quiet || json,
            ) {
                Ok(()) => {}
                Err(e) => {
                    if !json {
                        Output::warning(&format!("Agent context generation failed: {}", e));
                    }
                }
            }
        }
    }

    // Apply linkfiles and copyfiles after repos and composefiles
    match apply_links(workspace_root, manifest, quiet || json) {
        Ok(()) => {}
        Err(e) => {
            if !json {
                Output::warning(&format!("Link application failed: {}", e));
            }
        }
    }

    // Execute post-sync hooks
    let hook_results = if no_hooks {
        Vec::new()
    } else {
        execute_post_sync_hooks(workspace_root, manifest, &results, quiet, json)
    };

    if json {
        #[derive(serde::Serialize)]
        struct JsonSyncResult {
            success: bool,
            repos: Vec<JsonSyncRepo>,
            composefiles: usize,
            hooks: Vec<HookResult>,
        }

        let result = JsonSyncResult {
            success: error_count == 0,
            repos: json_repos,
            composefiles: composefiles_count,
            hooks: hook_results,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    }

    Ok(())
}

/// Sync gripspaces: update existing or clone new ones, then resolve merged manifest.
fn sync_gripspaces(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    quiet: bool,
) -> anyhow::Result<Manifest> {
    let gripspaces = match &manifest.gripspaces {
        Some(gs) if !gs.is_empty() => gs,
        _ => return Ok(manifest.clone()),
    };

    let spaces_dir = manifest_paths::spaces_dir(workspace_root);

    if !quiet {
        Output::header(&format!("Syncing {} gripspace(s)...", gripspaces.len()));
        println!();
    }

    for gs_config in gripspaces {
        let name = gripspace_name(&gs_config.url);
        // Use resolve_space_name to find the actual directory (handles reserved names)
        let dir_name = match crate::core::gripspace::resolve_space_name(&gs_config.url, &spaces_dir)
        {
            Ok(dir_name) => dir_name,
            Err(e) => {
                Output::warning(&format!(
                    "gripspace '{}': name resolution failed: {}",
                    gs_config.url, e
                ));
                continue;
            }
        };
        let gs_path = spaces_dir.join(&dir_name);

        if gs_path.exists() {
            match update_gripspace(&gs_path, gs_config) {
                Ok(()) => {
                    if !quiet {
                        Output::success(&format!("gripspace '{}': updated", name));
                    }
                }
                Err(e) => {
                    Output::warning(&format!("gripspace '{}': update failed: {}", name, e));
                }
            }
        } else {
            match ensure_gripspace(&spaces_dir, gs_config) {
                Ok(_) => {
                    if !quiet {
                        Output::success(&format!("gripspace '{}': cloned", name));
                    }
                }
                Err(e) => {
                    Output::warning(&format!("gripspace '{}': clone failed: {}", name, e));
                }
            }
        }
    }

    if !quiet {
        println!();
    }

    // Re-resolve the merged manifest from whichever layout is present.
    let manifest_path = manifest_paths::resolve_gripspace_manifest_path(workspace_root);
    let mut resolved = if let Some(path) = manifest_path {
        Manifest::parse_raw(&std::fs::read_to_string(path)?)?
    } else {
        manifest.clone()
    };

    if let Err(e) = resolve_all_gripspaces(&mut resolved, &spaces_dir) {
        Output::warning(&format!("Gripspace resolution failed: {}", e));
        return Ok(manifest.clone());
    }

    if let Err(e) = resolved.validate() {
        Output::warning(&format!(
            "Resolved manifest validation failed after gripspace sync: {}. \
Using the pre-sync manifest; check gripspace manifests/includes.",
            e
        ));
        return Ok(manifest.clone());
    }

    Ok(resolved)
}

/// Sync repos sequentially (original behavior)
fn sync_sequential(
    repos: &[RepoInfo],
    force: bool,
    quiet: bool,
    griptree_config: Option<&GriptreeConfig>,
    griptree_branch: Option<&str>,
    reset_refs: bool,
) -> anyhow::Result<Vec<SyncResult>> {
    let mut results = Vec::new();

    for repo in repos {
        let result = sync_single_repo(
            repo,
            force,
            quiet,
            true,
            griptree_config,
            griptree_branch,
            reset_refs,
        )?;
        results.push(result);
    }

    Ok(results)
}

/// Sync repos in parallel using tokio
#[allow(clippy::unnecessary_to_owned)] // We need to clone for move into spawn_blocking
async fn sync_parallel(
    repos: &[RepoInfo],
    force: bool,
    quiet: bool,
    griptree_config: Option<GriptreeConfig>,
    griptree_branch: Option<String>,
    reset_refs: bool,
) -> anyhow::Result<Vec<SyncResult>> {
    let results: Arc<Mutex<Vec<SyncResult>>> = Arc::new(Mutex::new(Vec::new()));
    let mut join_set: JoinSet<anyhow::Result<()>> = JoinSet::new();

    // Show a single spinner for all repos
    let spinner = Output::spinner(&format!("Syncing {} repos in parallel...", repos.len()));

    for repo in repos.to_vec() {
        let results = Arc::clone(&results);
        let griptree_config = griptree_config.clone();
        let griptree_branch = griptree_branch.clone();

        join_set.spawn_blocking(move || {
            let result = sync_single_repo(
                &repo,
                force,
                quiet,
                false,
                griptree_config.as_ref(),
                griptree_branch.as_deref(),
                reset_refs,
            )?;
            results.lock().expect("mutex poisoned").push(result);
            Ok(())
        });
    }

    // Wait for all tasks to complete
    while let Some(res) = join_set.join_next().await {
        res??;
    }

    spinner.finish_and_clear();

    // Extract results from Arc<Mutex<>>
    let results = match Arc::try_unwrap(results) {
        Ok(mutex) => mutex.into_inner().expect("mutex poisoned"),
        Err(arc) => arc.lock().expect("mutex poisoned").clone(),
    };

    // Print results in order
    for result in &results {
        if result.success {
            if !quiet || result.was_cloned {
                Output::success(&format!("{}: {}", result.name, result.message));
            }
        } else {
            Output::error(&format!("{}: {}", result.name, result.message));
        }
    }

    Ok(results)
}

fn sync_griptree_upstream(
    repo: &RepoInfo,
    git_repo: &Repository,
    current_branch: Option<&str>,
    griptree_config: Option<&GriptreeConfig>,
    spinner: Option<&ProgressBar>,
    quiet: bool,
) -> SyncResult {
    let upstream = match griptree_config {
        Some(cfg) => match cfg.upstream_for_repo(&repo.name, &repo.default_branch) {
            Ok(upstream) => upstream,
            Err(e) => {
                let msg = format!("error - {}", e);
                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                }
                return SyncResult {
                    name: repo.name.clone(),
                    success: false,
                    message: msg,
                    was_cloned: false,
                    had_changes: false,
                };
            }
        },
        None => format!("origin/{}", repo.default_branch),
    };

    let remote = upstream.split('/').next().unwrap_or("origin");
    let mut upstream_set_warning: Option<String> = None;

    if let Err(e) = fetch_remote(git_repo, remote) {
        let msg = format!("error - {}", e);
        if let Some(s) = spinner {
            s.finish_with_message(format!("{}: {}", repo.name, msg));
        }
        return SyncResult {
            name: repo.name.clone(),
            success: false,
            message: msg,
            was_cloned: false,
            had_changes: false,
        };
    }

    if let Some(current) = current_branch {
        if let Err(e) = set_branch_upstream_ref(git_repo, current, &upstream) {
            upstream_set_warning = Some(format!("upstream tracking not updated: {}", e));
        }
    }

    if let Some(current) = current_branch {
        match has_commits_ahead(git_repo, &upstream) {
            Ok(true) => {
                let mut msg = format!(
                    "skipped - branch '{}' has local commits not in '{}'",
                    current, upstream
                );
                if let Some(warning) = upstream_set_warning.as_ref() {
                    msg.push_str(&format!(" ({})", warning));
                }
                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                }
                return SyncResult {
                    name: repo.name.clone(),
                    success: true,
                    message: msg,
                    was_cloned: false,
                    had_changes: false,
                };
            }
            Ok(false) => {}
            Err(e) => {
                let msg = format!("error - {}", e);
                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                }
                return SyncResult {
                    name: repo.name.clone(),
                    success: false,
                    message: msg,
                    was_cloned: false,
                    had_changes: false,
                };
            }
        }
    }

    match pull_latest_from_upstream(git_repo, &upstream) {
        Ok(()) => {
            let mut msg = format!("pulled ({})", upstream);
            if let Some(warning) = upstream_set_warning.as_ref() {
                msg.push_str(&format!(" ({})", warning));
            }
            if let Some(s) = spinner {
                if !quiet {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                } else {
                    s.finish_and_clear();
                }
            }

            SyncResult {
                name: repo.name.clone(),
                success: true,
                message: msg,
                was_cloned: false,
                had_changes: true,
            }
        }
        Err(e) => {
            let msg = format!("error - {}", e);
            if let Some(s) = spinner {
                s.finish_with_message(format!("{}: {}", repo.name, msg));
            }
            SyncResult {
                name: repo.name.clone(),
                success: false,
                message: msg,
                was_cloned: false,
                had_changes: false,
            }
        }
    }
}

fn sync_reference_reset(
    repo: &RepoInfo,
    git_repo: &Repository,
    griptree_config: Option<&GriptreeConfig>,
    spinner: Option<&ProgressBar>,
    quiet: bool,
) -> SyncResult {
    let upstream = match griptree_config {
        Some(cfg) => match cfg.upstream_for_repo(&repo.name, &repo.default_branch) {
            Ok(upstream) => upstream,
            Err(e) => {
                let msg = format!("error - {}", e);
                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                }
                return SyncResult {
                    name: repo.name.clone(),
                    success: false,
                    message: msg,
                    was_cloned: false,
                    had_changes: false,
                };
            }
        },
        None => format!("origin/{}", repo.default_branch),
    };

    let mut upstream_parts = upstream.splitn(2, '/');
    let remote = upstream_parts.next().unwrap_or("origin");
    let upstream_branch = upstream_parts.next().unwrap_or(&repo.default_branch);

    if let Ok(is_dirty) = has_uncommitted_changes(git_repo) {
        if is_dirty {
            Output::warning(&format!(
                "{}: --reset-refs will discard local changes",
                repo.name
            ));
        }
    }
    if let Ok(true) = has_commits_ahead(git_repo, &upstream) {
        Output::warning(&format!(
            "{}: --reset-refs will discard local commits not in {}",
            repo.name, upstream
        ));
    }
    if let Err(e) = fetch_remote(git_repo, remote) {
        let msg = format!("error - {}", e);
        if let Some(s) = spinner {
            s.finish_with_message(format!("{}: {}", repo.name, msg));
        }
        return SyncResult {
            name: repo.name.clone(),
            success: false,
            message: msg,
            was_cloned: false,
            had_changes: false,
        };
    }

    let mut used_detached_fallback = false;
    if let Err(e) = checkout_branch_at_upstream(git_repo, upstream_branch, &upstream) {
        let err_msg = e.to_string();
        if is_worktree_branch_lock_error(&err_msg) {
            if let Err(detach_err) = checkout_detached(git_repo, &upstream) {
                let msg = format!(
                    "error - {} (fallback detach failed: {})",
                    err_msg, detach_err
                );
                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                }
                return SyncResult {
                    name: repo.name.clone(),
                    success: false,
                    message: msg,
                    was_cloned: false,
                    had_changes: false,
                };
            }
            used_detached_fallback = true;
        } else {
            let msg = format!("error - {}", err_msg);
            if let Some(s) = spinner {
                s.finish_with_message(format!("{}: {}", repo.name, msg));
            }
            return SyncResult {
                name: repo.name.clone(),
                success: false,
                message: msg,
                was_cloned: false,
                had_changes: false,
            };
        }
    }

    match reset_hard(git_repo, &upstream) {
        Ok(()) => {
            let msg = if used_detached_fallback {
                format!("reset ({}, detached fallback)", upstream)
            } else {
                format!("reset ({})", upstream)
            };
            if let Some(s) = spinner {
                if !quiet {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                } else {
                    s.finish_and_clear();
                }
            }

            SyncResult {
                name: repo.name.clone(),
                success: true,
                message: msg,
                was_cloned: false,
                had_changes: true,
            }
        }
        Err(e) => {
            let msg = format!("error - {}", e);
            if let Some(s) = spinner {
                s.finish_with_message(format!("{}: {}", repo.name, msg));
            }
            SyncResult {
                name: repo.name.clone(),
                success: false,
                message: msg,
                was_cloned: false,
                had_changes: false,
            }
        }
    }
}

fn is_worktree_branch_lock_error(message: &str) -> bool {
    message.contains("checked out in another worktree")
        || message.contains("already checked out in another worktree")
        || message.contains("already used by worktree")
}

/// Sync a single repository
fn sync_single_repo(
    repo: &RepoInfo,
    force: bool,
    quiet: bool,
    show_spinner: bool,
    griptree_config: Option<&GriptreeConfig>,
    griptree_branch: Option<&str>,
    reset_refs: bool,
) -> anyhow::Result<SyncResult> {
    let spinner = if show_spinner {
        Some(Output::spinner(&format!("Pulling {}...", repo.name)))
    } else {
        None
    };

    if !path_exists(&repo.absolute_path) {
        // Clone the repo
        if let Some(ref s) = spinner {
            s.set_message(format!("Cloning {}...", repo.name));
        }

        match clone_repo(&repo.url, &repo.absolute_path, Some(&repo.default_branch)) {
            Ok(_) => {
                // Check actual branch after clone
                let clone_msg = if let Ok(git_repo) = open_repo(&repo.absolute_path) {
                    if let Ok(actual_branch) = get_current_branch(&git_repo) {
                        if actual_branch != repo.default_branch {
                            format!(
                                "cloned (on '{}', manifest specifies '{}')",
                                actual_branch, repo.default_branch
                            )
                        } else {
                            "cloned".to_string()
                        }
                    } else {
                        "cloned".to_string()
                    }
                } else {
                    "cloned".to_string()
                };

                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, clone_msg));
                }

                return Ok(SyncResult {
                    name: repo.name.clone(),
                    success: true,
                    message: clone_msg,
                    was_cloned: true,
                    had_changes: true,
                });
            }
            Err(e) => {
                let msg = format!("clone failed - {}", e);
                if let Some(s) = spinner {
                    s.finish_with_message(format!("{}: {}", repo.name, msg));
                }
                return Ok(SyncResult {
                    name: repo.name.clone(),
                    success: false,
                    message: msg,
                    was_cloned: false,
                    had_changes: false,
                });
            }
        }
    }

    // Pull existing repo
    match open_repo(&repo.absolute_path) {
        Ok(git_repo) => {
            if repo.reference && reset_refs {
                let result =
                    sync_reference_reset(repo, &git_repo, griptree_config, spinner.as_ref(), quiet);
                return Ok(result);
            }

            let current_branch = get_current_branch(&git_repo).ok();
            let use_griptree_upstream = match (griptree_branch, current_branch.as_deref()) {
                (Some(base), Some(current)) => current == base,
                _ => false,
            };

            if use_griptree_upstream {
                let result = sync_griptree_upstream(
                    repo,
                    &git_repo,
                    current_branch.as_deref(),
                    griptree_config,
                    spinner.as_ref(),
                    quiet,
                );
                Ok(result)
            } else {
                let result = safe_pull_latest(&git_repo, &repo.default_branch, "origin");

                match result {
                    Ok(pull_result) => {
                        let had_changes = pull_result.pulled;
                        let (success, message) = if pull_result.pulled {
                            if pull_result.recovered {
                                (
                                    true,
                                    pull_result
                                        .message
                                        .unwrap_or_else(|| "pulled (recovered)".to_string()),
                                )
                            } else {
                                (
                                    true,
                                    pull_result.message.unwrap_or_else(|| "pulled".to_string()),
                                )
                            }
                        } else if let Some(msg) = pull_result.message {
                            if force {
                                (true, format!("skipped - {}", msg))
                            } else {
                                (true, msg)
                            }
                        } else {
                            (true, "up to date".to_string())
                        };

                        if let Some(s) = spinner {
                            if !quiet || !success {
                                s.finish_with_message(format!("{}: {}", repo.name, message));
                            } else {
                                s.finish_and_clear();
                            }
                        }

                        Ok(SyncResult {
                            name: repo.name.clone(),
                            success,
                            message,
                            was_cloned: false,
                            had_changes,
                        })
                    }
                    Err(e) => {
                        let msg = format!("error - {}", e);
                        if let Some(s) = spinner {
                            s.finish_with_message(format!("{}: {}", repo.name, msg));
                        }
                        Ok(SyncResult {
                            name: repo.name.clone(),
                            success: false,
                            message: msg,
                            was_cloned: false,
                            had_changes: false,
                        })
                    }
                }
            }
        }
        Err(e) => {
            let msg = format!("error - {}", e);
            if let Some(s) = spinner {
                s.finish_with_message(format!("{}: {}", repo.name, msg));
            }
            Ok(SyncResult {
                name: repo.name.clone(),
                success: false,
                message: msg,
                was_cloned: false,
                had_changes: false,
            })
        }
    }
}

// Make SyncResult cloneable for parallel sync
impl Clone for SyncResult {
    fn clone(&self) -> Self {
        SyncResult {
            name: self.name.clone(),
            success: self.success,
            message: self.message.clone(),
            was_cloned: self.was_cloned,
            had_changes: self.had_changes,
        }
    }
}

/// Execute post-sync hooks defined in the manifest
fn execute_post_sync_hooks(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    sync_results: &[SyncResult],
    quiet: bool,
    json: bool,
) -> Vec<HookResult> {
    let hooks = match manifest
        .workspace
        .as_ref()
        .and_then(|w| w.hooks.as_ref())
        .and_then(|h| h.post_sync.as_ref())
    {
        Some(hooks) if !hooks.is_empty() => hooks,
        _ => return Vec::new(),
    };

    // Build set of repos that had changes
    let changed_repos: HashSet<&str> = sync_results
        .iter()
        .filter(|r| r.had_changes)
        .map(|r| r.name.as_str())
        .collect();

    let any_changed = !changed_repos.is_empty();

    if !quiet && !json {
        Output::header("Post-Sync Hooks");
        println!();
    }

    let mut results = Vec::new();

    for hook in hooks {
        let hook_name = hook.name.as_deref().unwrap_or(&hook.command).to_string();

        // Check condition
        let should_run = match hook.condition {
            HookCondition::Always => true,
            HookCondition::Changed => {
                if let Some(ref repos) = hook.repos {
                    // Run if any of the specified repos had changes
                    repos.iter().any(|r| changed_repos.contains(r.as_str()))
                } else {
                    // Run if any repo had changes
                    any_changed
                }
            }
        };

        if !should_run {
            if !quiet && !json {
                Output::info(&format!("{}: skipped (no changes)", hook_name));
            }
            results.push(HookResult {
                name: hook_name,
                success: true,
                skipped: true,
                duration_ms: 0,
                error: None,
            });
            continue;
        }

        let working_dir = hook
            .cwd
            .as_ref()
            .map(|p| workspace_root.join(p))
            .unwrap_or_else(|| workspace_root.clone());

        let start = std::time::Instant::now();
        let status = std::process::Command::new("sh")
            .arg("-c")
            .arg(&hook.command)
            .current_dir(&working_dir)
            .status();
        let duration_ms = start.elapsed().as_millis() as u64;

        match status {
            Ok(exit) if exit.success() => {
                if !quiet && !json {
                    Output::success(&format!(
                        "{}: completed ({:.1}s)",
                        hook_name,
                        duration_ms as f64 / 1000.0
                    ));
                }
                results.push(HookResult {
                    name: hook_name,
                    success: true,
                    skipped: false,
                    duration_ms,
                    error: None,
                });
            }
            Ok(exit) => {
                let err_msg = format!("exit code {}", exit.code().unwrap_or(-1));
                if !quiet && !json {
                    Output::warning(&format!("{}: failed ({})", hook_name, err_msg));
                }
                results.push(HookResult {
                    name: hook_name,
                    success: false,
                    skipped: false,
                    duration_ms,
                    error: Some(err_msg),
                });
            }
            Err(e) => {
                let err_msg = e.to_string();
                if !quiet && !json {
                    Output::warning(&format!("{}: failed ({})", hook_name, err_msg));
                }
                results.push(HookResult {
                    name: hook_name,
                    success: false,
                    skipped: false,
                    duration_ms,
                    error: Some(err_msg),
                });
            }
        }
    }

    if !quiet && !json && !results.is_empty() {
        println!();
    }

    results
}

/// Rollback all repos to their state before the last sync.
pub async fn run_sync_rollback(
    workspace_root: &Path,
    _manifest: &Manifest,
    quiet: bool,
    json: bool,
) -> anyhow::Result<()> {
    let snapshot = SyncSnapshot::load_latest(workspace_root)?
        .ok_or_else(|| anyhow::anyhow!("No sync snapshot found. Run `gr sync` first."))?;

    if !quiet && !json {
        Output::header(&format!(
            "Rolling back to snapshot from {}",
            snapshot.timestamp.format("%Y-%m-%d %H:%M:%S UTC")
        ));
        println!();
    }

    let mut success_count = 0;
    let mut error_count = 0;

    #[derive(serde::Serialize)]
    struct JsonRollbackRepo {
        name: String,
        action: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    }
    let mut json_repos: Vec<JsonRollbackRepo> = Vec::new();

    for repo_snap in &snapshot.repos {
        if !repo_snap.path.exists() {
            if !quiet && !json {
                Output::warning(&format!("{}: skipped (not on disk)", repo_snap.name));
            }
            json_repos.push(JsonRollbackRepo {
                name: repo_snap.name.clone(),
                action: "skipped".to_string(),
                error: Some("not on disk".to_string()),
            });
            continue;
        }

        match open_repo(&repo_snap.path) {
            Ok(git_repo) => {
                // Checkout the recorded branch first (if different)
                let current = get_current_branch(&git_repo).unwrap_or_default();
                if current != repo_snap.branch {
                    if let Err(e) =
                        crate::git::branch::checkout_branch(&git_repo, &repo_snap.branch)
                    {
                        let msg = format!("checkout failed: {}", e);
                        if !quiet && !json {
                            Output::error(&format!("{}: {}", repo_snap.name, msg));
                        }
                        error_count += 1;
                        json_repos.push(JsonRollbackRepo {
                            name: repo_snap.name.clone(),
                            action: "failed".to_string(),
                            error: Some(msg),
                        });
                        continue;
                    }
                }

                // Reset to the recorded commit
                match reset_hard(&git_repo, &repo_snap.head_commit) {
                    Ok(()) => {
                        if !quiet && !json {
                            Output::success(&format!(
                                "{}: restored to {} on {}",
                                repo_snap.name,
                                &repo_snap.head_commit[..7.min(repo_snap.head_commit.len())],
                                repo_snap.branch
                            ));
                        }
                        success_count += 1;
                        json_repos.push(JsonRollbackRepo {
                            name: repo_snap.name.clone(),
                            action: "restored".to_string(),
                            error: None,
                        });
                    }
                    Err(e) => {
                        let msg = format!("reset failed: {}", e);
                        if !quiet && !json {
                            Output::error(&format!("{}: {}", repo_snap.name, msg));
                        }
                        error_count += 1;
                        json_repos.push(JsonRollbackRepo {
                            name: repo_snap.name.clone(),
                            action: "failed".to_string(),
                            error: Some(msg),
                        });
                    }
                }
            }
            Err(e) => {
                let msg = format!("open failed: {}", e);
                if !quiet && !json {
                    Output::error(&format!("{}: {}", repo_snap.name, msg));
                }
                error_count += 1;
                json_repos.push(JsonRollbackRepo {
                    name: repo_snap.name.clone(),
                    action: "failed".to_string(),
                    error: Some(msg),
                });
            }
        }
    }

    if json {
        #[derive(serde::Serialize)]
        struct JsonRollbackResult {
            success: bool,
            timestamp: String,
            repos: Vec<JsonRollbackRepo>,
        }
        let result = JsonRollbackResult {
            success: error_count == 0,
            timestamp: snapshot.timestamp.to_rfc3339(),
            repos: json_repos,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else if !quiet {
        println!();
        if error_count == 0 {
            Output::success(&format!("Rolled back {} repo(s).", success_count));
        } else {
            Output::warning(&format!(
                "{} restored, {} failed",
                success_count, error_count
            ));
        }
    }

    Ok(())
}

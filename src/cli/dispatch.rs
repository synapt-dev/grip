//! Command dispatch logic
//!
//! Routes parsed CLI commands to their respective handlers.

use clap::CommandFactory;
use clap_complete::generate;
use colored::Colorize;

use super::args::{
    AgentCommands, CiCommands, Cli, Commands, GroupCommands, IssueCommands, ManifestCommands,
    McpCommands, PrCommands, RepoCommands, SpawnCommands, TargetCommands, TreeCommands,
};
use super::context::WorkspaceContext;

/// Dispatch a parsed CLI command to its handler.
pub async fn dispatch_command(
    command: Option<Commands>,
    quiet: bool,
    verbose: bool,
    json: bool,
) -> anyhow::Result<()> {
    match command {
        Some(Commands::Status {
            verbose: status_verbose,
            group,
            repo,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::status::run_status(
                &ctx.workspace_root,
                &ctx.manifest,
                status_verbose,
                ctx.quiet,
                repo.as_deref(),
                group.as_deref(),
                ctx.json,
            )?;
        }
        Some(Commands::Sync {
            force,
            reset_refs,
            group,
            repo,
            sequential,
            no_hooks,
            rollback,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            if rollback {
                crate::cli::commands::sync::run_sync_rollback(
                    &ctx.workspace_root,
                    &ctx.manifest,
                    ctx.quiet,
                    ctx.json,
                )
                .await?;
            } else {
                crate::cli::commands::sync::run_sync(
                    &ctx.workspace_root,
                    &ctx.manifest,
                    force,
                    ctx.quiet,
                    repo.as_deref(),
                    group.as_deref(),
                    sequential,
                    reset_refs,
                    ctx.json,
                    no_hooks,
                )
                .await?;
            }
        }
        Some(Commands::Branch {
            name,
            delete,
            r#move,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::branch::run_branch(
                crate::cli::commands::branch::BranchOptions {
                    workspace_root: &ctx.workspace_root,
                    manifest: &ctx.manifest,
                    name: name.as_deref(),
                    delete,
                    move_commits: r#move,
                    repos_filter: repo.as_deref(),
                    group_filter: group.as_deref(),
                    json: ctx.json,
                },
            )?;
        }
        Some(Commands::Checkout {
            name,
            create,
            base,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            let branch = if base {
                let config = crate::core::griptree::GriptreeConfig::load_from_workspace(
                    &ctx.workspace_root,
                )?
                .ok_or_else(|| anyhow::anyhow!("Not in a griptree workspace"))?;
                config.branch
            } else {
                name.ok_or_else(|| anyhow::anyhow!("Branch name is required"))?
            };

            crate::cli::commands::checkout::run_checkout(
                &ctx.workspace_root,
                &ctx.manifest,
                &branch,
                create,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Add { files, repo, group }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::add::run_add(
                &ctx.workspace_root,
                &ctx.manifest,
                &files,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Restore {
            files,
            staged,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::restore::run_restore(
                &ctx.workspace_root,
                &ctx.manifest,
                &files,
                staged,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Diff {
            staged,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::diff::run_diff(
                &ctx.workspace_root,
                &ctx.manifest,
                staged,
                ctx.json,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Commit {
            message,
            amend,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            let msg = message.unwrap_or_else(|| {
                eprintln!("Error: commit message required (-m)");
                std::process::exit(1);
            });
            crate::cli::commands::commit::run_commit(
                &ctx.workspace_root,
                &ctx.manifest,
                &msg,
                amend,
                ctx.json,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Push {
            set_upstream,
            force,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::push::run_push(
                &ctx.workspace_root,
                &ctx.manifest,
                set_upstream,
                force,
                ctx.quiet,
                ctx.json,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Prune {
            execute,
            remote,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::prune::run_prune(
                &ctx.workspace_root,
                &ctx.manifest,
                execute,
                remote,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Pr { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                PrCommands::Create {
                    title,
                    body,
                    push,
                    draft,
                    dry_run,
                    repo,
                } => {
                    crate::cli::commands::pr::run_pr_create(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        title.as_deref(),
                        body.as_deref(),
                        draft,
                        push,
                        dry_run,
                        repo.as_deref(),
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Status => {
                    crate::cli::commands::pr::run_pr_status(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Merge {
                    method,
                    force,
                    update,
                    auto,
                    wait,
                    timeout,
                    no_delete_branch,
                } => {
                    crate::cli::commands::pr::run_pr_merge(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        &crate::cli::commands::pr::MergeOptions {
                            method: method.as_ref(),
                            force,
                            update,
                            auto,
                            json: ctx.json,
                            wait,
                            timeout,
                            delete_branch: !no_delete_branch,
                        },
                    )
                    .await?;
                }
                PrCommands::Edit { title, body } => {
                    crate::cli::commands::pr::run_pr_edit(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        title.as_deref(),
                        body.as_deref(),
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Checks { repo } => {
                    crate::cli::commands::pr::run_pr_checks(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::Diff { stat } => {
                    crate::cli::commands::pr::run_pr_diff(&ctx.workspace_root, &ctx.manifest, stat)
                        .await?;
                }
                PrCommands::List { state, repo, limit } => {
                    crate::cli::commands::pr::run_pr_list(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        state,
                        repo.as_deref(),
                        limit,
                        ctx.json,
                    )
                    .await?;
                }
                PrCommands::View { number, repo } => {
                    crate::cli::commands::pr::run_pr_view(
                        crate::cli::commands::pr::PRViewOptions {
                            workspace_root: &ctx.workspace_root,
                            manifest: &ctx.manifest,
                            number,
                            repo_filter: repo.as_deref(),
                            json_output: ctx.json,
                        },
                    )
                    .await?;
                }
            }
        }
        Some(Commands::Init {
            url,
            path,
            from_dirs,
            dirs,
            interactive,
            no_interactive,
            create_manifest,
            manifest_name,
            private,
            from_repo,
        }) => {
            use std::io::IsTerminal;
            let effective_interactive = if no_interactive {
                false
            } else if interactive {
                true
            } else {
                from_dirs && std::io::stdin().is_terminal()
            };
            crate::cli::commands::init::run_init(crate::cli::commands::init::InitOptions {
                url: url.as_deref(),
                path: path.as_deref(),
                from_dirs,
                dirs: &dirs,
                interactive: effective_interactive,
                create_manifest,
                manifest_name: manifest_name.as_deref(),
                private,
                from_repo,
            })
            .await?;
        }
        Some(Commands::Tree { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                TreeCommands::Add { branch } => {
                    crate::cli::commands::tree::run_tree_add(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        &branch,
                    )?;
                }
                TreeCommands::List => {
                    crate::cli::commands::tree::run_tree_list(&ctx.workspace_root)?;
                }
                TreeCommands::Remove { branch, force } => {
                    crate::cli::commands::tree::run_tree_remove(
                        &ctx.workspace_root,
                        &branch,
                        force,
                    )?;
                }
                TreeCommands::Lock { branch, reason } => {
                    crate::cli::commands::tree::run_tree_lock(
                        &ctx.workspace_root,
                        &branch,
                        reason.as_deref(),
                    )?;
                }
                TreeCommands::Unlock { branch } => {
                    crate::cli::commands::tree::run_tree_unlock(&ctx.workspace_root, &branch)?;
                }
                TreeCommands::Return {
                    base,
                    no_sync,
                    autostash,
                    prune,
                    prune_current,
                    prune_remote,
                    force,
                } => {
                    crate::cli::commands::tree::run_tree_return(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        &crate::cli::commands::tree::TreeReturnOptions {
                            base_override: base.as_deref(),
                            no_sync,
                            autostash,
                            prune_branch: prune.as_deref(),
                            prune_current,
                            prune_remote,
                            force,
                        },
                    )
                    .await?;
                }
            }
        }
        Some(Commands::Grep {
            pattern,
            ignore_case,
            parallel,
            pathspec,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::grep::run_grep(
                &ctx.workspace_root,
                &ctx.manifest,
                &pattern,
                ignore_case,
                parallel,
                &pathspec,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Forall {
            command,
            parallel,
            all,
            no_intercept,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::forall::run_forall(
                &ctx.workspace_root,
                &ctx.manifest,
                &command,
                parallel,
                !all, // Default: only repos with changes (changed_only=true unless --all)
                no_intercept,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Rebase {
            onto,
            upstream,
            abort,
            continue_rebase,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::rebase::run_rebase(
                &ctx.workspace_root,
                &ctx.manifest,
                onto.as_deref(),
                upstream,
                abort,
                continue_rebase,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Pull {
            rebase,
            repo,
            group,
            sequential,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::pull::run_pull(
                &ctx.workspace_root,
                &ctx.manifest,
                rebase,
                repo.as_deref(),
                group.as_deref(),
                sequential,
                ctx.quiet,
            )
            .await?;
        }
        Some(Commands::Link { status, apply }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::link::run_link(
                &ctx.workspace_root,
                &ctx.manifest,
                status,
                apply,
                ctx.json,
            )?;
        }
        Some(Commands::Run { name, list }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::run::run_run(
                &ctx.workspace_root,
                &ctx.manifest,
                name.as_deref(),
                list,
            )?;
        }
        Some(Commands::Env) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::env::run_env(&ctx.workspace_root, &ctx.manifest)?;
        }
        Some(Commands::Repo { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                RepoCommands::List => {
                    crate::cli::commands::repo::run_repo_list(&ctx.workspace_root, &ctx.manifest)?;
                }
                RepoCommands::Add {
                    url,
                    path,
                    branch,
                    target,
                } => {
                    crate::cli::commands::repo::run_repo_add(
                        &ctx.workspace_root,
                        &url,
                        path.as_deref(),
                        branch.as_deref(),
                        target.as_deref(),
                    )?;
                }
                RepoCommands::Remove { name, delete } => {
                    crate::cli::commands::repo::run_repo_remove(
                        &ctx.workspace_root,
                        &name,
                        delete,
                    )?;
                }
            }
        }
        Some(Commands::Group { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                GroupCommands::List => {
                    crate::cli::commands::group::run_group_list(
                        &ctx.workspace_root,
                        &ctx.manifest,
                    )?;
                }
                GroupCommands::Add { group, repos } => {
                    crate::cli::commands::group::run_group_add(
                        &ctx.workspace_root,
                        &group,
                        &repos,
                    )?;
                }
                GroupCommands::Remove { group, repos } => {
                    crate::cli::commands::group::run_group_remove(
                        &ctx.workspace_root,
                        &group,
                        &repos,
                    )?;
                }
                GroupCommands::Create { name } => {
                    crate::cli::commands::group::run_group_create(&ctx.workspace_root, &name)?;
                }
            }
        }
        Some(Commands::Target { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                TargetCommands::List => {
                    crate::cli::commands::target::run_target_list(
                        &ctx.workspace_root,
                        &ctx.manifest,
                    )?;
                }
                TargetCommands::Set { branch, repo } => {
                    if let Some(repo_name) = repo {
                        crate::cli::commands::target::run_target_set_repo(
                            &ctx.workspace_root,
                            &repo_name,
                            &branch,
                        )?;
                    } else {
                        crate::cli::commands::target::run_target_set(&ctx.workspace_root, &branch)?;
                    }
                }
                TargetCommands::Unset { repo } => {
                    if let Some(repo_name) = repo {
                        crate::cli::commands::target::run_target_unset_repo(
                            &ctx.workspace_root,
                            &repo_name,
                        )?;
                    } else {
                        crate::cli::commands::target::run_target_unset(&ctx.workspace_root)?;
                    }
                }
            }
        }
        Some(Commands::Gc {
            aggressive,
            dry_run,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::gc::run_gc(
                &ctx.workspace_root,
                &ctx.manifest,
                aggressive,
                dry_run,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::CherryPick {
            commit,
            abort,
            continue_pick,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::cherry_pick::run_cherry_pick(
                &ctx.workspace_root,
                &ctx.manifest,
                commit.as_deref(),
                abort,
                continue_pick,
                repo.as_deref(),
                group.as_deref(),
            )?;
        }
        Some(Commands::Ci { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                CiCommands::Run { name } => {
                    crate::cli::commands::ci::run_ci_run(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        &name,
                        ctx.json,
                    )?;
                }
                CiCommands::List => {
                    crate::cli::commands::ci::run_ci_list(&ctx.manifest, ctx.json)?;
                }
                CiCommands::Status => {
                    crate::cli::commands::ci::run_ci_status(&ctx.workspace_root, ctx.json)?;
                }
            }
        }
        Some(Commands::Manifest { action }) => match action {
            ManifestCommands::Import { path, output } => {
                crate::cli::commands::manifest::run_manifest_import(&path, output.as_deref())?;
            }
            ManifestCommands::Sync => {
                let ctx = load_workspace_context(quiet, verbose, json)?;
                crate::cli::commands::manifest::run_manifest_sync(&ctx.workspace_root)?;
            }
            ManifestCommands::Schema { format } => {
                crate::cli::commands::manifest::run_manifest_schema(&format)?;
            }
        },
        Some(Commands::Bench(args)) => {
            crate::cli::commands::bench::run(args).await?;
        }
        Some(Commands::Agent { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                AgentCommands::Context { repo } => {
                    crate::cli::commands::agent::run_agent_context(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                        ctx.json,
                    )?;
                }
                AgentCommands::Build { repo } => {
                    crate::cli::commands::agent::run_agent_build(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                    )?;
                }
                AgentCommands::Test { repo } => {
                    crate::cli::commands::agent::run_agent_test(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                    )?;
                }
                AgentCommands::Verify { repo } => {
                    crate::cli::commands::agent::run_agent_verify(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                    )?;
                }
                AgentCommands::GenerateContext { dry_run } => {
                    crate::cli::commands::agent::run_agent_generate_context(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        dry_run,
                        ctx.quiet,
                    )?;
                }
            }
        }
        Some(Commands::Spawn { action }) => match action {
            SpawnCommands::Up {
                agent,
                config,
                mock,
            } => {
                crate::cli::commands::spawn::run_spawn_up(agent, config, mock, quiet, json)?;
            }
            SpawnCommands::Status => {
                crate::cli::commands::spawn::run_spawn_status(quiet, json)?;
            }
            SpawnCommands::Down { agent } => {
                crate::cli::commands::spawn::run_spawn_down(agent, quiet, json)?;
            }
            SpawnCommands::List => {
                crate::cli::commands::spawn::run_spawn_list(quiet, json)?;
            }
        },
        Some(Commands::Issue { action }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            match action {
                IssueCommands::List {
                    repo,
                    state,
                    label,
                    assignee,
                    limit,
                } => {
                    crate::cli::commands::issue::run_issue_list(
                        &crate::cli::commands::issue::IssueListOptions {
                            workspace_root: &ctx.workspace_root,
                            manifest: &ctx.manifest,
                            repo_filter: repo.as_deref(),
                            state: &state,
                            labels: label.as_deref(),
                            assignee: assignee.as_deref(),
                            limit,
                            json: ctx.json,
                        },
                    )
                    .await?;
                }
                IssueCommands::Create {
                    repo,
                    title,
                    body,
                    label,
                    assignee,
                } => {
                    crate::cli::commands::issue::run_issue_create(
                        &crate::cli::commands::issue::IssueCreateCommandOptions {
                            workspace_root: &ctx.workspace_root,
                            manifest: &ctx.manifest,
                            repo_filter: repo.as_deref(),
                            title: &title,
                            body: body.as_deref(),
                            labels: label.as_deref(),
                            assignees: assignee.as_deref(),
                            json: ctx.json,
                        },
                    )
                    .await?;
                }
                IssueCommands::View { number, repo } => {
                    crate::cli::commands::issue::run_issue_view(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                        number,
                        ctx.json,
                    )
                    .await?;
                }
                IssueCommands::Close { number, repo } => {
                    crate::cli::commands::issue::run_issue_close(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                        number,
                        ctx.json,
                    )
                    .await?;
                }
                IssueCommands::Reopen { number, repo } => {
                    crate::cli::commands::issue::run_issue_reopen(
                        &ctx.workspace_root,
                        &ctx.manifest,
                        repo.as_deref(),
                        number,
                        ctx.json,
                    )
                    .await?;
                }
            }
        }
        Some(Commands::Mcp { action }) => match action {
            McpCommands::Server => {
                crate::mcp::server::run_mcp_server()?;
            }
        },
        Some(Commands::Release {
            version,
            notes,
            dry_run,
            skip_pr,
            repo,
            timeout,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::release::run_release(
                crate::cli::commands::release::ReleaseOptions {
                    workspace_root: &ctx.workspace_root,
                    manifest: &ctx.manifest,
                    version: &version,
                    notes: notes.as_deref(),
                    dry_run,
                    skip_pr,
                    target_repo: repo.as_deref(),
                    json: ctx.json,
                    quiet: ctx.quiet,
                    timeout,
                },
            )
            .await?;
        }
        Some(Commands::Completions { shell }) => {
            let mut cmd = Cli::command();
            generate(shell, &mut cmd, "gr", &mut std::io::stdout());
        }
        Some(Commands::Verify {
            clean,
            links,
            on_branch,
            synced,
            repo,
            group,
        }) => {
            let ctx = load_workspace_context(quiet, verbose, json)?;
            crate::cli::commands::verify::run_verify(
                crate::cli::commands::verify::VerifyOptions {
                    workspace_root: &ctx.workspace_root,
                    manifest: &ctx.manifest,
                    repos_filter: repo.as_deref(),
                    group_filter: group.as_deref(),
                    json: ctx.json,
                    quiet: ctx.quiet,
                    clean,
                    links,
                    on_branch: on_branch.as_deref(),
                    synced,
                },
            )?;
        }
        None => {
            let logo = r#"
                *    *
           *    |    |
           |    |    |
      *    |    |    |
      |    \    \    \
       \    \    \    \       *
        *    *    *    *       \
         \    \    \    \      *
          \    \    \    \    /
           *----*----*----*--*
            \        |      /
             \       |     /
              *------*----*
"#;
            let wordmark = r#"
        __ _ _ _              _
       / _` (_) |_ __ _ _ _ _(_)_ __
      | (_| | |  _/ _` | '_| | '_ \
       \__, |_|\__\__, |_| |_| .__/
       |___/      |___/      |_|
"#;
            print!("{}", logo.truecolor(251, 146, 60));
            print!("{}", wordmark.bold());
            println!("{}", "              git a grip.".truecolor(251, 146, 60));
            println!();
            println!("  Run {} for usage", "'gr --help'".dimmed());
        }
    }

    Ok(())
}

/// Load the gripspace manifest
fn load_gripspace() -> anyhow::Result<(std::path::PathBuf, crate::core::manifest::Manifest)> {
    let current = std::env::current_dir()?;

    // First, check if we're in a griptree (has .griptree pointer file)
    if let Some((griptree_path, pointer)) =
        crate::core::griptree::GriptreePointer::find_in_ancestors(&current)
    {
        return load_from_griptree(&griptree_path, &pointer);
    }

    // Not in a griptree - search parent directories for workspace root
    load_from_workspace(&current)
}

/// Load the gripspace manifest and return a WorkspaceContext with global CLI flags.
fn load_workspace_context(
    quiet: bool,
    verbose: bool,
    json: bool,
) -> anyhow::Result<WorkspaceContext> {
    let (workspace_root, manifest) = load_gripspace()?;
    Ok(WorkspaceContext::new(
        workspace_root,
        manifest,
        quiet,
        verbose,
        json,
    ))
}

/// Load manifest from a griptree workspace.
fn load_from_griptree(
    griptree_path: &std::path::Path,
    pointer: &crate::core::griptree::GriptreePointer,
) -> anyhow::Result<(std::path::PathBuf, crate::core::manifest::Manifest)> {
    let griptree_manifest_path =
        crate::core::manifest_paths::resolve_gripspace_manifest_path(griptree_path);

    let content = if let Some(path) = griptree_manifest_path {
        std::fs::read_to_string(path)?
    } else {
        let main_workspace = std::path::PathBuf::from(&pointer.main_workspace);
        let main_manifest_path =
            crate::core::manifest_paths::resolve_gripspace_manifest_path(&main_workspace);

        let main_path = main_manifest_path.ok_or_else(|| {
            anyhow::anyhow!(
                "Griptree points to main workspace '{}' but no gripspace manifest was found",
                pointer.main_workspace
            )
        })?;
        std::fs::read_to_string(main_path)?
    };

    let mut manifest = crate::core::manifest::Manifest::parse(&content)?;
    resolve_gripspace_includes(&mut manifest, griptree_path);
    Ok((griptree_path.to_path_buf(), manifest))
}

/// Search parent directories for a workspace root and load its manifest.
fn load_from_workspace(
    start: &std::path::Path,
) -> anyhow::Result<(std::path::PathBuf, crate::core::manifest::Manifest)> {
    let mut search_path = start.to_path_buf();
    loop {
        // Check for .gitgrip directory with gripspace manifest
        let gitgrip_dir = search_path.join(".gitgrip");
        if gitgrip_dir.exists() {
            if let Some(manifest_path) =
                crate::core::manifest_paths::resolve_gripspace_manifest_path(&search_path)
            {
                let content = std::fs::read_to_string(&manifest_path)?;
                let mut manifest = crate::core::manifest::Manifest::parse(&content)?;
                resolve_gripspace_includes(&mut manifest, &search_path);
                return Ok((search_path, manifest));
            }
        }

        // Check for legacy repo.yaml
        if let Some(repo_yaml) =
            crate::core::manifest_paths::resolve_repo_manifest_path(&search_path)
        {
            let content = std::fs::read_to_string(repo_yaml)?;
            let mut manifest = crate::core::manifest::Manifest::parse(&content)?;
            resolve_gripspace_includes(&mut manifest, &search_path);
            return Ok((search_path, manifest));
        }

        // Fallback: parse .repo/manifest.xml directly (zero-config — just works)
        let repo_xml = search_path.join(".repo").join("manifest.xml");
        if repo_xml.exists() {
            let xml_manifest = crate::core::repo_manifest::XmlManifest::parse_file(&repo_xml)?;
            let result = xml_manifest.to_manifest()?;
            return Ok((search_path, result.manifest));
        }

        match search_path.parent() {
            Some(parent) => search_path = parent.to_path_buf(),
            None => {
                anyhow::bail!("Not in a gitgrip workspace (no .gitgrip or .repo directory found)");
            }
        }
    }
}

/// Resolve gripspace includes (merge inherited repos/scripts/env/hooks) if spaces dir exists.
fn resolve_gripspace_includes(
    manifest: &mut crate::core::manifest::Manifest,
    workspace_root: &std::path::Path,
) {
    let spaces_dir = crate::core::manifest_paths::spaces_dir(workspace_root);
    if spaces_dir.exists() {
        let _ = crate::core::gripspace::resolve_all_gripspaces(manifest, &spaces_dir);
    }
}

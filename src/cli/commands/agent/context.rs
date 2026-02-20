//! Agent context command — dump workspace context for AI agent system prompts.

use std::path::PathBuf;

use crate::core::griptree::GriptreeConfig;
use crate::core::manifest::Manifest;
use crate::core::repo::filter_repos;
use crate::git::status::get_repo_status;

use super::{
    AgentContextJson, GriptreeContextJson, RepoAgentContextJson, RepoContextJson,
    WorkspaceContextJson,
};

/// Run the agent context command.
///
/// Outputs workspace context as plain markdown (for system prompt injection)
/// or structured JSON (for programmatic consumption).
pub fn run_agent_context(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    repo_filter: Option<&str>,
    json: bool,
) -> anyhow::Result<()> {
    // Get all repos (include reference repos)
    let all_repos = filter_repos(manifest, workspace_root, None, None, true);

    // Validate repo filter
    if let Some(filter) = repo_filter {
        if !all_repos.iter().any(|r| r.name == filter) {
            anyhow::bail!("Repository '{}' not found in manifest", filter);
        }
    }

    // Get status for each repo
    let statuses: Vec<_> = all_repos.iter().map(|r| (r, get_repo_status(r))).collect();

    // Load griptree config (if in a griptree workspace)
    let griptree = GriptreeConfig::load_from_workspace(workspace_root)
        .ok()
        .flatten();

    if json {
        output_json(workspace_root, manifest, &statuses, &griptree, repo_filter)
    } else {
        output_markdown(workspace_root, manifest, &statuses, &griptree, repo_filter);
        Ok(())
    }
}

fn output_json(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    statuses: &[(&crate::core::repo::RepoInfo, crate::git::status::RepoStatus)],
    griptree: &Option<GriptreeConfig>,
    repo_filter: Option<&str>,
) -> anyhow::Result<()> {
    let ws_agent = manifest.workspace.as_ref().and_then(|w| w.agent.as_ref());

    let scripts: Option<Vec<String>> = manifest
        .workspace
        .as_ref()
        .and_then(|w| w.scripts.as_ref())
        .map(|s| s.keys().cloned().collect());

    let env = manifest.workspace.as_ref().and_then(|w| w.env.clone());

    let workspace = WorkspaceContextJson {
        root: workspace_root.display().to_string(),
        description: ws_agent.and_then(|a| a.description.clone()),
        conventions: ws_agent.map(|a| a.conventions.clone()).unwrap_or_default(),
        workflows: ws_agent.and_then(|a| a.workflows.clone()),
        scripts,
        env,
    };

    let repos: Vec<RepoContextJson> = statuses
        .iter()
        .filter(|(repo, _)| repo_filter.map_or(true, |f| repo.name == f))
        .map(|(repo, status)| RepoContextJson {
            name: repo.name.clone(),
            path: repo.path.clone(),
            url: repo.url.clone(),
            default_branch: repo.revision.clone(),
            current_branch: status.branch.clone(),
            clean: status.clean,
            exists: status.exists,
            reference: repo.reference,
            groups: repo.groups.clone(),
            agent: repo.agent.as_ref().map(|a| RepoAgentContextJson {
                description: a.description.clone(),
                language: a.language.clone(),
                build: a.build.clone(),
                test: a.test.clone(),
                lint: a.lint.clone(),
                format: a.format.clone(),
            }),
        })
        .collect();

    let griptree_json = griptree.as_ref().map(|g| GriptreeContextJson {
        branch: g.branch.clone(),
        path: g.path.clone(),
        upstreams: g.repo_upstreams.clone(),
    });

    let context = AgentContextJson {
        workspace,
        repos,
        griptree: griptree_json,
    };

    println!("{}", serde_json::to_string_pretty(&context)?);
    Ok(())
}

fn output_markdown(
    workspace_root: &PathBuf,
    manifest: &Manifest,
    statuses: &[(&crate::core::repo::RepoInfo, crate::git::status::RepoStatus)],
    griptree: &Option<GriptreeConfig>,
    repo_filter: Option<&str>,
) {
    let ws_agent = manifest.workspace.as_ref().and_then(|w| w.agent.as_ref());

    // Workspace header
    println!("# Workspace: {}", workspace_root.display());
    if let Some(desc) = ws_agent.and_then(|a| a.description.as_deref()) {
        println!("{}", desc);
    }
    println!();

    // Conventions
    if let Some(agent) = ws_agent {
        if !agent.conventions.is_empty() {
            println!("## Conventions");
            for convention in &agent.conventions {
                println!("- {}", convention);
            }
            println!();
        }

        // Workflows
        if let Some(workflows) = &agent.workflows {
            if !workflows.is_empty() {
                println!("## Workflows");
                for (name, cmd) in workflows {
                    println!("- {}: `{}`", name, cmd);
                }
                println!();
            }
        }
    }

    // Scripts
    if let Some(workspace) = &manifest.workspace {
        if let Some(scripts) = &workspace.scripts {
            if !scripts.is_empty() {
                println!("## Scripts");
                for name in scripts.keys() {
                    println!("- {}", name);
                }
                println!();
            }
        }
    }

    // Griptree section
    if let Some(g) = griptree {
        println!("## Griptree: {}", g.branch);
        if !g.repo_upstreams.is_empty() {
            let upstreams: Vec<String> = g
                .repo_upstreams
                .iter()
                .map(|(repo, upstream)| format!("{}:{}", repo, upstream))
                .collect();
            println!("Upstreams: {}", upstreams.join(", "));
        }
        println!();
    }

    // Repos
    println!("## Repos");
    println!();

    for (repo, status) in statuses {
        if let Some(filter) = repo_filter {
            if repo.name != filter {
                continue;
            }
        }

        // Repo header line
        let ref_tag = if repo.reference { " [reference]" } else { "" };
        let agent = repo.agent.as_ref();

        let lang = agent
            .and_then(|a| a.language.as_deref())
            .map(|l| format!(" ({})", l))
            .unwrap_or_default();

        let desc = agent
            .and_then(|a| a.description.as_deref())
            .map(|d| format!(" -- {}", d))
            .unwrap_or_default();

        // Build command annotations
        let mut cmds = Vec::new();
        if let Some(a) = agent {
            if let Some(b) = &a.build {
                cmds.push(format!("build: {}", b));
            }
            if let Some(t) = &a.test {
                cmds.push(format!("test: {}", t));
            }
            if let Some(l) = &a.lint {
                cmds.push(format!("lint: {}", l));
            }
        }
        let cmds_str = if cmds.is_empty() {
            String::new()
        } else {
            format!(" [{}]", cmds.join("] ["))
        };

        println!("### {}{}{}{}", repo.name, lang, ref_tag, desc);

        if !status.exists {
            println!("- Status: not cloned");
        } else {
            // Status line
            let status_str = if status.clean {
                "clean".to_string()
            } else {
                let mut parts = Vec::new();
                if status.staged > 0 {
                    parts.push(format!("{} staged", status.staged));
                }
                if status.modified > 0 {
                    parts.push(format!("{} modified", status.modified));
                }
                if status.untracked > 0 {
                    parts.push(format!("{} untracked", status.untracked));
                }
                parts.join(", ")
            };

            println!("- Branch: {} (default: {})", status.branch, repo.revision);
            println!("- Status: {}", status_str);
        }

        if !cmds_str.is_empty() {
            println!("- Commands:{}", cmds_str);
        }

        if !repo.groups.is_empty() {
            println!("- Groups: {}", repo.groups.join(", "));
        }

        println!();
    }
}

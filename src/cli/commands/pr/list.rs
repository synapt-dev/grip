//! PR list command implementation

use crate::cli::args::PrStateFilter;
use crate::cli::output::{Output, Table};
use crate::core::manifest::Manifest;
use crate::core::repo::RepoInfo;
use crate::git::path_exists;
use crate::platform::get_platform_adapter;
use crate::platform::types::{PRListFilter, PRState};
use std::path::Path;

/// Run the PR list command
pub async fn run_pr_list(
    workspace_root: &Path,
    manifest: &Manifest,
    state: PrStateFilter,
    repo_filter: Option<&str>,
    limit: u32,
    json_output: bool,
) -> anyhow::Result<()> {
    let filter_state = match state {
        PrStateFilter::Open => Some(PRState::Open),
        PrStateFilter::Closed => Some(PRState::Closed),
        PrStateFilter::Merged => Some(PRState::Merged),
        PrStateFilter::All => None,
    };

    if !json_output {
        let state_label = match state {
            PrStateFilter::Open => "open",
            PrStateFilter::Closed => "closed",
            PrStateFilter::Merged => "merged",
            PrStateFilter::All => "all",
        };
        Output::header(&format!("Pull Requests ({})", state_label));
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
        .filter(|r| !r.reference)
        .filter(|r| repo_filter.map(|f| r.name == f).unwrap_or(true))
        .collect();

    if let Some(name) = repo_filter {
        if repos.is_empty() {
            anyhow::bail!("Repository '{}' not found in manifest", name);
        }
    }

    #[derive(serde::Serialize)]
    struct PRListEntry {
        repo: String,
        number: u64,
        title: String,
        state: String,
        head: String,
        base: String,
        url: String,
    }

    let mut all_prs: Vec<PRListEntry> = Vec::new();

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let platform = get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

        let filter = PRListFilter {
            state: filter_state,
            limit: Some(limit),
        };

        match platform
            .list_pull_requests(&repo.owner, &repo.repo, &filter)
            .await
        {
            Ok(prs) => {
                for pr in prs {
                    all_prs.push(PRListEntry {
                        repo: repo.name.clone(),
                        number: pr.number,
                        title: pr.title,
                        state: pr.state.to_string(),
                        head: pr.head.ref_name,
                        base: pr.base.ref_name,
                        url: pr.url,
                    });
                }
            }
            Err(e) => {
                if !json_output {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
            }
        }
    }

    if json_output {
        println!("{}", serde_json::to_string_pretty(&all_prs)?);
        return Ok(());
    }

    if all_prs.is_empty() {
        println!("No pull requests found.");
        return Ok(());
    }

    let mut table = Table::new(vec!["Repo", "PR#", "Title", "State", "Branch"]);

    for pr in &all_prs {
        table.add_row(vec![
            &pr.repo,
            &format!("#{}", pr.number),
            &pr.title,
            &pr.state,
            &pr.head,
        ]);
    }

    table.print();

    println!();
    println!("{} pull request(s)", all_prs.len());

    Ok(())
}

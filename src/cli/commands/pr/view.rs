//! PR view command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::RepoInfo;
use crate::git::path_exists;
use crate::platform::get_platform_adapter;
use std::path::Path;

/// Options for the PR view command
pub struct PRViewOptions<'a> {
    pub workspace_root: &'a Path,
    pub manifest: &'a Manifest,
    pub number: Option<u64>,
    pub repo_filter: Option<&'a str>,
    pub json_output: bool,
}

/// Run the PR view command
pub async fn run_pr_view(opts: PRViewOptions<'_>) -> anyhow::Result<()> {
    let repos: Vec<RepoInfo> = opts
        .manifest
        .repos
        .iter()
        .filter_map(|(name, config)| {
            RepoInfo::from_config(
                name,
                config,
                opts.workspace_root,
                &opts.manifest.settings,
                opts.manifest.remotes.as_ref(),
            )
        })
        .filter(|r| !r.reference)
        .filter(|r| opts.repo_filter.map(|f| r.name == f).unwrap_or(true))
        .collect();

    if let Some(name) = opts.repo_filter {
        if repos.is_empty() {
            anyhow::bail!("Repository '{}' not found in manifest", name);
        }
    }

    // If no PR number is given, find PRs for the current branch
    let number = opts.number;

    #[derive(serde::Serialize)]
    struct PRViewInfo {
        repo: String,
        number: u64,
        title: String,
        state: String,
        merged: bool,
        head: String,
        base: String,
        body: String,
        url: String,
        mergeable: Option<bool>,
        reviews: Vec<ReviewInfo>,
    }

    #[derive(serde::Serialize)]
    struct ReviewInfo {
        user: String,
        state: String,
    }

    let mut all_views: Vec<PRViewInfo> = Vec::new();

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let platform = get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

        let pr_number = if let Some(n) = number {
            n
        } else {
            // Find PR by current branch
            let git_repo = match crate::git::open_repo(&repo.absolute_path) {
                Ok(r) => r,
                Err(_) => continue,
            };
            let branch = match crate::git::get_current_branch(&git_repo) {
                Ok(b) => b,
                Err(_) => continue,
            };
            if branch == repo.target_branch() {
                continue;
            }
            match platform
                .find_pr_by_branch(&repo.owner, &repo.repo, &branch)
                .await
            {
                Ok(Some(pr)) => pr.number,
                Ok(None) => {
                    if !opts.json_output {
                        Output::info(&format!(
                            "{}: no open PR for branch '{}'",
                            repo.name, branch
                        ));
                    }
                    continue;
                }
                Err(e) => {
                    if !opts.json_output {
                        Output::error(&format!("{}: {}", repo.name, e));
                    }
                    continue;
                }
            }
        };

        match platform
            .get_pull_request(&repo.owner, &repo.repo, pr_number)
            .await
        {
            Ok(pr) => {
                let reviews = platform
                    .get_pull_request_reviews(&repo.owner, &repo.repo, pr_number)
                    .await
                    .unwrap_or_default();

                if !opts.json_output {
                    let state_str = if pr.merged {
                        "merged"
                    } else {
                        &pr.state.to_string()
                    };

                    println!("=== {} #{} ===", repo.name, pr.number);
                    println!("Title:     {}", pr.title);
                    println!("State:     {}", state_str);
                    println!("Branch:    {} -> {}", pr.head.ref_name, pr.base.ref_name);
                    println!("URL:       {}", pr.url);

                    if let Some(mergeable) = pr.mergeable {
                        println!("Mergeable: {}", if mergeable { "yes" } else { "no" });
                    }

                    if !reviews.is_empty() {
                        println!("Reviews:");
                        for review in &reviews {
                            println!("  {} ({})", review.user, review.state);
                        }
                    }

                    if !pr.body.is_empty() {
                        println!();
                        println!("{}", pr.body);
                    }

                    println!();
                }

                all_views.push(PRViewInfo {
                    repo: repo.name.clone(),
                    number: pr.number,
                    title: pr.title,
                    state: pr.state.to_string(),
                    merged: pr.merged,
                    head: pr.head.ref_name,
                    base: pr.base.ref_name,
                    body: pr.body,
                    url: pr.url,
                    mergeable: pr.mergeable,
                    reviews: reviews
                        .into_iter()
                        .map(|r| ReviewInfo {
                            user: r.user,
                            state: r.state,
                        })
                        .collect(),
                });
            }
            Err(e) => {
                if !opts.json_output {
                    Output::error(&format!("{}: {}", repo.name, e));
                }
            }
        }
    }

    if opts.json_output {
        println!("{}", serde_json::to_string_pretty(&all_views)?);
    } else if all_views.is_empty() {
        println!("No pull request found.");
    }

    Ok(())
}

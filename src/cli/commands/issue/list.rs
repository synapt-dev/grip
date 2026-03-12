//! Issue list command implementation

use crate::cli::output::{Output, Table};
use crate::core::manifest::Manifest;
use crate::platform::types::{IssueListFilter, IssueState};
use std::path::Path;

use super::{get_adapter, resolve_target_repo};

/// Options for the issue list command
pub struct IssueListOptions<'a> {
    pub workspace_root: &'a Path,
    pub manifest: &'a Manifest,
    pub repo_filter: Option<&'a str>,
    pub state: &'a str,
    pub labels: Option<&'a [String]>,
    pub assignee: Option<&'a str>,
    pub limit: u32,
    pub json: bool,
}

/// Run the issue list command
pub async fn run_issue_list(opts: &IssueListOptions<'_>) -> anyhow::Result<()> {
    let repo = resolve_target_repo(opts.workspace_root, opts.manifest, opts.repo_filter)?;
    let adapter = get_adapter(&repo);

    let issue_state = match opts.state {
        "closed" => Some(IssueState::Closed),
        "all" => None,
        _ => Some(IssueState::Open),
    };

    let filter = IssueListFilter {
        state: issue_state,
        labels: opts.labels.map(|l| l.to_vec()).unwrap_or_default(),
        assignee: opts.assignee.map(|s| s.to_string()),
        limit: Some(opts.limit),
    };

    let issues = adapter
        .list_issues(&repo.owner, &repo.repo, &filter)
        .await?;

    if opts.json {
        println!("{}", serde_json::to_string_pretty(&issues)?);
        return Ok(());
    }

    Output::header(&format!("Issues — {}/{}", repo.owner, repo.repo));
    println!();

    if issues.is_empty() {
        let state_label = match issue_state {
            Some(IssueState::Open) => "open ",
            Some(IssueState::Closed) => "closed ",
            None => "",
        };
        println!("  No {}issues found.", state_label);
        return Ok(());
    }

    let mut table = Table::new(vec!["#", "Title", "State", "Labels", "Author"]);

    for issue in &issues {
        let labels_str = issue
            .labels
            .iter()
            .map(|l| l.name.as_str())
            .collect::<Vec<_>>()
            .join(", ");

        table.add_row(vec![
            &format!("#{}", issue.number),
            &issue.title,
            &issue.state.to_string(),
            &labels_str,
            &issue.author,
        ]);
    }

    table.print();
    println!();
    println!("  {} issue(s) shown", issues.len());

    Ok(())
}

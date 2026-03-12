//! Issue create command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::platform::types::IssueCreateOptions;
use std::path::Path;

use super::{get_adapter, resolve_target_repo};

/// Options for the issue create command
pub struct IssueCreateCommandOptions<'a> {
    pub workspace_root: &'a Path,
    pub manifest: &'a Manifest,
    pub repo_filter: Option<&'a str>,
    pub title: &'a str,
    pub body: Option<&'a str>,
    pub labels: Option<&'a [String]>,
    pub assignees: Option<&'a [String]>,
    pub json: bool,
}

/// Run the issue create command
pub async fn run_issue_create(opts: &IssueCreateCommandOptions<'_>) -> anyhow::Result<()> {
    let repo = resolve_target_repo(opts.workspace_root, opts.manifest, opts.repo_filter)?;
    let adapter = get_adapter(&repo);

    let options = IssueCreateOptions {
        title: opts.title.to_string(),
        body: opts.body.map(|s| s.to_string()),
        labels: opts.labels.map(|l| l.to_vec()).unwrap_or_default(),
        assignees: opts.assignees.map(|a| a.to_vec()).unwrap_or_default(),
    };

    let result = adapter
        .create_issue(&repo.owner, &repo.repo, &options)
        .await?;

    if opts.json {
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    Output::success(&format!(
        "Created issue #{} on {}/{}",
        result.number, repo.owner, repo.repo
    ));
    println!("  {}", result.url);

    Ok(())
}

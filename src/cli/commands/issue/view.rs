//! Issue view command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use std::path::Path;

use super::{get_adapter, resolve_target_repo};

/// Run the issue view command
pub async fn run_issue_view(
    workspace_root: &Path,
    manifest: &Manifest,
    repo_filter: Option<&str>,
    issue_number: u64,
    json_output: bool,
) -> anyhow::Result<()> {
    let repo = resolve_target_repo(workspace_root, manifest, repo_filter)?;
    let adapter = get_adapter(&repo);

    let issue = adapter
        .get_issue(&repo.owner, &repo.repo, issue_number)
        .await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&issue)?);
        return Ok(());
    }

    Output::header(&format!("Issue #{} — {}", issue.number, issue.title));
    println!();

    Output::kv("State", &Output::status(&issue.state.to_string()));
    Output::kv("Author", &issue.author);
    Output::kv("URL", &issue.url);

    if !issue.labels.is_empty() {
        let labels: Vec<&str> = issue.labels.iter().map(|l| l.name.as_str()).collect();
        Output::kv("Labels", &labels.join(", "));
    }

    if !issue.assignees.is_empty() {
        Output::kv("Assignees", &issue.assignees.join(", "));
    }

    Output::kv("Created", &issue.created_at);
    Output::kv("Updated", &issue.updated_at);

    if !issue.body.is_empty() {
        println!();
        println!("{}", issue.body);
    }

    Ok(())
}

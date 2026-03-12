//! Issue close command implementation

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use std::path::Path;

use super::{get_adapter, resolve_target_repo};

/// Run the issue close command
pub async fn run_issue_close(
    workspace_root: &Path,
    manifest: &Manifest,
    repo_filter: Option<&str>,
    issue_number: u64,
    json_output: bool,
) -> anyhow::Result<()> {
    let repo = resolve_target_repo(workspace_root, manifest, repo_filter)?;
    let adapter = get_adapter(&repo);

    adapter
        .close_issue(&repo.owner, &repo.repo, issue_number)
        .await?;

    if json_output {
        println!(
            "{}",
            serde_json::json!({
                "repo": format!("{}/{}", repo.owner, repo.repo),
                "issue": issue_number,
                "action": "closed"
            })
        );
        return Ok(());
    }

    Output::success(&format!(
        "Closed issue #{} on {}/{}",
        issue_number, repo.owner, repo.repo
    ));

    Ok(())
}

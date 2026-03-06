//! Agent test command — run agent.test for repo(s).

use std::path::Path;
use std::process::Command;

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::filter_repos;

/// Run the agent test command.
///
/// Executes the `agent.test` command from the manifest for each repo
/// (or a specific repo if `repo_filter` is set).
pub fn run_agent_test(
    workspace_root: &Path,
    manifest: &Manifest,
    repo_filter: Option<&str>,
) -> anyhow::Result<()> {
    let repos = filter_repos(manifest, workspace_root, None, None, false);

    let mut ran_any = false;

    for repo in &repos {
        if let Some(filter) = repo_filter {
            if repo.name != filter {
                continue;
            }
        }

        let test_cmd = repo.agent.as_ref().and_then(|a| a.test.as_deref());

        let Some(cmd) = test_cmd else {
            if repo_filter.is_some() {
                anyhow::bail!(
                    "Repository '{}' has no agent.test command defined in the manifest",
                    repo.name
                );
            }
            continue;
        };

        Output::header(&format!("Testing {}", repo.name));
        Output::info(&format!("$ {}", cmd));

        let status = Command::new("sh")
            .arg("-c")
            .arg(cmd)
            .current_dir(&repo.absolute_path)
            .status()?;

        if !status.success() {
            anyhow::bail!(
                "Tests failed for '{}' (exit code: {:?})",
                repo.name,
                status.code()
            );
        }

        Output::success(&format!("{} tests passed", repo.name));
        ran_any = true;
    }

    if !ran_any {
        if repo_filter.is_some() {
            anyhow::bail!("Repository not found in manifest");
        }
        Output::info("No repos have agent.test configured");
    }

    Ok(())
}

//! Agent verify command — run all checks (build + test + lint) for repo(s).

use std::path::Path;
use std::process::Command;

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::filter_repos;

/// Run the agent verify command.
///
/// Executes build, test, and lint commands from the manifest for each repo
/// that has them configured. Continues through failures and reports a summary.
pub fn run_agent_verify(
    workspace_root: &Path,
    manifest: &Manifest,
    repo_filter: Option<&str>,
) -> anyhow::Result<()> {
    let repos = filter_repos(manifest, workspace_root, None, None, false);

    let mut total_pass = 0;
    let mut total_fail = 0;
    let mut total_skip = 0;

    for repo in &repos {
        if let Some(filter) = repo_filter {
            if repo.name != filter {
                continue;
            }
        }

        let agent = match &repo.agent {
            Some(a) => a,
            None => {
                if repo_filter.is_some() {
                    anyhow::bail!(
                        "Repository '{}' has no agent config defined in the manifest",
                        repo.name
                    );
                }
                continue;
            }
        };

        let checks: Vec<(&str, Option<&str>)> = vec![
            ("build", agent.build.as_deref()),
            ("test", agent.test.as_deref()),
            ("lint", agent.lint.as_deref()),
        ];

        for (label, cmd_opt) in &checks {
            let Some(cmd) = cmd_opt else {
                total_skip += 1;
                continue;
            };

            Output::info(&format!("[{}] {} -> {}", repo.name, label, cmd));

            let status = Command::new("sh")
                .arg("-c")
                .arg(cmd)
                .current_dir(&repo.absolute_path)
                .status()?;

            if status.success() {
                Output::success(&format!("[{}] {} passed", repo.name, label));
                total_pass += 1;
            } else {
                Output::error(&format!("[{}] {} failed", repo.name, label));
                total_fail += 1;
            }
        }
    }

    // Summary
    println!();
    if total_fail > 0 {
        Output::error(&format!(
            "Verification: {} passed, {} failed, {} skipped",
            total_pass, total_fail, total_skip
        ));
        anyhow::bail!("{} verification check(s) failed", total_fail);
    } else if total_pass > 0 {
        Output::success(&format!(
            "Verification: all {} checks passed ({} skipped)",
            total_pass, total_skip
        ));
    } else {
        Output::info("No repos have agent checks configured");
    }

    Ok(())
}

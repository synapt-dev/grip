//! Verify command implementation
//!
//! Provides boolean pass/fail assertions for agents.
//! Exit code 0 = pass, 1 = fail. With --json, always exits 0
//! and puts pass/fail in the JSON body.

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::git::path_exists;
use crate::git::status::get_repo_status;
use std::path::{Path, PathBuf};

/// JSON-serializable verification result
#[derive(serde::Serialize)]
struct VerifyResult {
    pass: bool,
    checks: Vec<CheckResult>,
}

/// JSON-serializable individual check result
#[derive(serde::Serialize)]
struct CheckResult {
    name: String,
    pass: bool,
    details: Vec<serde_json::Value>,
}

/// Options for the verify command
pub struct VerifyOptions<'a> {
    pub workspace_root: &'a PathBuf,
    pub manifest: &'a Manifest,
    pub repos_filter: Option<&'a [String]>,
    pub group_filter: Option<&'a [String]>,
    pub json: bool,
    pub quiet: bool,
    pub clean: bool,
    pub links: bool,
    pub on_branch: Option<&'a str>,
    pub synced: bool,
}

/// Run the verify command
pub fn run_verify(opts: VerifyOptions) -> anyhow::Result<()> {
    let has_any_check = opts.clean || opts.links || opts.on_branch.is_some() || opts.synced;

    if !has_any_check {
        if opts.json {
            let result = VerifyResult {
                pass: false,
                checks: vec![CheckResult {
                    name: "no-checks".to_string(),
                    pass: false,
                    details: vec![serde_json::json!({
                        "error": "No verification flags provided. Use --clean, --links, --on-branch, or --synced."
                    })],
                }],
            };
            println!("{}", serde_json::to_string_pretty(&result)?);
            return Ok(());
        }
        anyhow::bail!(
            "No verification flags provided. Use --clean, --links, --on-branch, or --synced."
        );
    }

    let repos: Vec<RepoInfo> = filter_repos(
        opts.manifest,
        opts.workspace_root,
        opts.repos_filter,
        opts.group_filter,
        false,
    );

    let mut all_checks: Vec<CheckResult> = Vec::new();

    if opts.clean {
        all_checks.push(check_clean(&repos));
    }

    if opts.links {
        all_checks.push(check_links(opts.workspace_root, opts.manifest));
    }

    if let Some(branch) = opts.on_branch {
        all_checks.push(check_on_branch(&repos, branch));
    }

    if opts.synced {
        all_checks.push(check_synced(&repos));
    }

    let all_pass = all_checks.iter().all(|c| c.pass);

    if opts.json {
        let result = VerifyResult {
            pass: all_pass,
            checks: all_checks,
        };
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    // Human-readable output
    if !opts.quiet {
        Output::header("Verify");
        println!();
    }

    for check in &all_checks {
        if check.pass {
            Output::success(&format!("{}: passed", check.name));
        } else {
            Output::error(&format!("{}: failed", check.name));
            for detail in &check.details {
                if let Some(obj) = detail.as_object() {
                    let parts: Vec<String> = obj
                        .iter()
                        .map(|(k, v)| format!("{}={}", k, v.as_str().unwrap_or(&v.to_string())))
                        .collect();
                    println!("    {}", parts.join(", "));
                }
            }
        }
    }

    if !opts.quiet {
        println!();
    }

    if !all_pass {
        std::process::exit(1);
    }

    Ok(())
}

/// Check that all repos are clean (no uncommitted changes)
fn check_clean(repos: &[RepoInfo]) -> CheckResult {
    let mut details = Vec::new();
    let mut pass = true;

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            details.push(serde_json::json!({
                "repo": repo.name,
                "status": "not cloned"
            }));
            pass = false;
            continue;
        }

        let status = get_repo_status(repo);
        if !status.clean {
            details.push(serde_json::json!({
                "repo": repo.name,
                "staged": status.staged,
                "modified": status.modified,
                "untracked": status.untracked
            }));
            pass = false;
        }
    }

    CheckResult {
        name: "clean".to_string(),
        pass,
        details,
    }
}

/// Check that all file links (copyfile/linkfile) are valid
fn check_links(workspace_root: &Path, manifest: &Manifest) -> CheckResult {
    let mut details = Vec::new();
    let mut broken = 0;

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
        .collect();

    for (name, config) in &manifest.repos {
        let repo = repos.iter().find(|r| &r.name == name);

        if let Some(ref copyfiles) = config.copyfile {
            for copyfile in copyfiles {
                let source = repo
                    .map(|r| r.absolute_path.join(&copyfile.src))
                    .unwrap_or_else(|| workspace_root.join(&config.path).join(&copyfile.src));
                let dest = workspace_root.join(&copyfile.dest);

                if !source.exists() || !dest.exists() {
                    broken += 1;
                    let reason = if !source.exists() {
                        "source missing"
                    } else {
                        "dest missing"
                    };
                    details.push(serde_json::json!({
                        "type": "copyfile",
                        "src": copyfile.src,
                        "dest": copyfile.dest,
                        "reason": reason
                    }));
                }
            }
        }

        if let Some(ref linkfiles) = config.linkfile {
            for linkfile in linkfiles {
                let source = repo
                    .map(|r| r.absolute_path.join(&linkfile.src))
                    .unwrap_or_else(|| workspace_root.join(&config.path).join(&linkfile.src));
                let dest = workspace_root.join(&linkfile.dest);

                let is_valid = source.exists() && dest.exists() && dest.is_symlink();
                if !is_valid {
                    broken += 1;
                    let reason = if !source.exists() {
                        "source missing"
                    } else if !dest.exists() {
                        "link missing"
                    } else {
                        "not a symlink"
                    };
                    details.push(serde_json::json!({
                        "type": "linkfile",
                        "src": linkfile.src,
                        "dest": linkfile.dest,
                        "reason": reason
                    }));
                }
            }
        }
    }

    CheckResult {
        name: "links".to_string(),
        pass: broken == 0,
        details,
    }
}

/// Check that all non-reference repos are on the expected branch
fn check_on_branch(repos: &[RepoInfo], expected_branch: &str) -> CheckResult {
    let mut details = Vec::new();
    let mut pass = true;

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        let status = get_repo_status(repo);
        if status.branch != expected_branch {
            details.push(serde_json::json!({
                "repo": repo.name,
                "expected": expected_branch,
                "actual": status.branch
            }));
            pass = false;
        }
    }

    CheckResult {
        name: "on-branch".to_string(),
        pass,
        details,
    }
}

/// Check that all repos are synced with their remote (not ahead or behind)
fn check_synced(repos: &[RepoInfo]) -> CheckResult {
    let mut details = Vec::new();
    let mut pass = true;

    for repo in repos {
        if !path_exists(&repo.absolute_path) {
            details.push(serde_json::json!({
                "repo": repo.name,
                "status": "not cloned"
            }));
            pass = false;
            continue;
        }

        let status = get_repo_status(repo);
        if status.ahead > 0 || status.behind > 0 {
            details.push(serde_json::json!({
                "repo": repo.name,
                "ahead": status.ahead,
                "behind": status.behind
            }));
            pass = false;
        }
    }

    CheckResult {
        name: "synced".to_string(),
        pass,
        details,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::manifest::PlatformType;

    fn make_repo_info(name: &str, path: &std::path::Path) -> RepoInfo {
        RepoInfo {
            name: name.to_string(),
            url: format!("file://{}", path.display()),
            path: name.to_string(),
            absolute_path: path.to_path_buf(),
            revision: "main".to_string(),
            target: "main".to_string(),
            sync_remote: "origin".to_string(),
            push_remote: "origin".to_string(),
            owner: "local".to_string(),
            repo: name.to_string(),
            platform_type: PlatformType::GitHub,
            platform_base_url: None,
            project: None,
            reference: false,
            groups: Vec::new(),
            agent: None, clone_strategy: crate::core::manifest::CloneStrategy::Clone,
        }
    }

    #[test]
    fn test_check_clean_with_clean_repos() {
        let temp = tempfile::TempDir::new().unwrap();
        let repo_path = temp.path().join("repo1");

        // Init a clean repo
        std::fs::create_dir_all(&repo_path).unwrap();
        std::process::Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::fs::write(repo_path.join("README.md"), "# test").unwrap();
        std::process::Command::new("git")
            .args(["add", "."])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        let repos = vec![make_repo_info("repo1", &repo_path)];
        let result = check_clean(&repos);
        assert!(result.pass);
        assert!(result.details.is_empty());
    }

    #[test]
    fn test_check_clean_with_dirty_repos() {
        let temp = tempfile::TempDir::new().unwrap();
        let repo_path = temp.path().join("repo1");

        // Init repo with uncommitted changes
        std::fs::create_dir_all(&repo_path).unwrap();
        std::process::Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::fs::write(repo_path.join("README.md"), "# test").unwrap();
        std::process::Command::new("git")
            .args(["add", "."])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        // Create an untracked file
        std::fs::write(repo_path.join("dirty.txt"), "dirty").unwrap();

        let repos = vec![make_repo_info("repo1", &repo_path)];
        let result = check_clean(&repos);
        assert!(!result.pass);
        assert_eq!(result.details.len(), 1);
        assert_eq!(result.details[0]["repo"], "repo1");
    }

    #[test]
    fn test_check_on_branch_matching() {
        let temp = tempfile::TempDir::new().unwrap();
        let repo_path = temp.path().join("repo1");

        std::fs::create_dir_all(&repo_path).unwrap();
        std::process::Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::fs::write(repo_path.join("README.md"), "# test").unwrap();
        std::process::Command::new("git")
            .args(["add", "."])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        let repos = vec![make_repo_info("repo1", &repo_path)];
        let result = check_on_branch(&repos, "main");
        assert!(result.pass);
        assert!(result.details.is_empty());
    }

    #[test]
    fn test_check_on_branch_mismatch() {
        let temp = tempfile::TempDir::new().unwrap();
        let repo_path = temp.path().join("repo1");

        std::fs::create_dir_all(&repo_path).unwrap();
        std::process::Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.email", "test@test.com"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["config", "user.name", "Test"])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::fs::write(repo_path.join("README.md"), "# test").unwrap();
        std::process::Command::new("git")
            .args(["add", "."])
            .current_dir(&repo_path)
            .output()
            .unwrap();
        std::process::Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&repo_path)
            .output()
            .unwrap();

        let repos = vec![make_repo_info("repo1", &repo_path)];
        let result = check_on_branch(&repos, "feat/something");
        assert!(!result.pass);
        assert_eq!(result.details.len(), 1);
        assert_eq!(result.details[0]["expected"], "feat/something");
        assert_eq!(result.details[0]["actual"], "main");
    }
}

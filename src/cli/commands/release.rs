//! Release command implementation
//!
//! Automates the full release workflow: version bump, changelog, build,
//! branch, commit, push, PR, CI wait, merge, and GitHub release creation.

use std::path::PathBuf;
use std::process::Command;

use chrono::Local;
use serde::Serialize;

use crate::cli::output::Output;
use crate::core::manifest::Manifest;
use crate::core::repo::{filter_repos, RepoInfo};
use crate::platform::{get_platform_adapter, ReleaseResult};

/// Options for the release command
pub struct ReleaseOptions<'a> {
    pub workspace_root: &'a PathBuf,
    pub manifest: &'a Manifest,
    pub version: &'a str,
    pub notes: Option<&'a str>,
    pub dry_run: bool,
    pub skip_pr: bool,
    pub target_repo: Option<&'a str>,
    pub json: bool,
    pub quiet: bool,
    pub timeout: u64,
}

/// JSON output for release steps
#[derive(Serialize)]
struct ReleaseOutputJson {
    version: String,
    steps: Vec<StepResultJson>,
}

#[derive(Serialize)]
struct StepResultJson {
    name: String,
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    files: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    number: Option<u64>,
}

impl StepResultJson {
    fn ok(name: &str) -> Self {
        Self {
            name: name.to_string(),
            status: "ok".to_string(),
            files: None,
            url: None,
            number: None,
        }
    }

    fn skipped(name: &str) -> Self {
        Self {
            name: name.to_string(),
            status: "skipped".to_string(),
            files: None,
            url: None,
            number: None,
        }
    }
}

/// Normalize version string — strip leading 'v' for internal use, ensure tag has 'v' prefix
pub fn normalize_version(version: &str) -> anyhow::Result<(String, String)> {
    let bare = version.strip_prefix('v').unwrap_or(version);

    // Basic semver validation: must have at least X.Y.Z
    let parts: Vec<&str> = bare.split('.').collect();
    if parts.len() < 2 {
        anyhow::bail!(
            "Invalid version '{}'. Expected format: X.Y.Z (e.g. 0.12.4)",
            version
        );
    }
    for part in &parts[..2] {
        if part.parse::<u64>().is_err() {
            anyhow::bail!(
                "Invalid version '{}'. Version components must be numeric.",
                version
            );
        }
    }

    let tag = format!("v{}", bare);
    Ok((bare.to_string(), tag))
}

/// Auto-detect version files in repo directories
pub fn detect_version_files(
    workspace_root: &PathBuf,
    repos: &[RepoInfo],
) -> Vec<(String, PathBuf)> {
    let mut files = Vec::new();

    for repo in repos {
        if repo.reference {
            continue;
        }

        let cargo_toml = repo.absolute_path.join("Cargo.toml");
        if cargo_toml.exists() {
            files.push((repo.name.clone(), cargo_toml));
        }

        let package_json = repo.absolute_path.join("package.json");
        if package_json.exists() {
            files.push((repo.name.clone(), package_json));
        }
    }

    // Also check workspace root
    let root_cargo = workspace_root.join("Cargo.toml");
    if root_cargo.exists() && !files.iter().any(|(_, p)| p == &root_cargo) {
        files.push(("workspace".to_string(), root_cargo));
    }

    let root_package = workspace_root.join("package.json");
    if root_package.exists() && !files.iter().any(|(_, p)| p == &root_package) {
        files.push(("workspace".to_string(), root_package));
    }

    files
}

/// Bump version in a Cargo.toml file
pub fn bump_cargo_toml(path: &PathBuf, new_version: &str, dry_run: bool) -> anyhow::Result<bool> {
    let content = std::fs::read_to_string(path)?;

    // Match the first `version = "..."` in the [package] section
    let re = regex::Regex::new(r#"(?m)^(version\s*=\s*")([^"]+)(")"#)?;
    if !re.is_match(&content) {
        return Ok(false);
    }

    let old_version = re
        .captures(&content)
        .and_then(|c| c.get(2))
        .map(|m| m.as_str().to_string())
        .unwrap_or_default();

    if old_version == new_version {
        return Ok(false); // Already at this version
    }

    let new_content = re
        .replace(&content, format!("${{1}}{}${{3}}", new_version))
        .to_string();

    if !dry_run {
        std::fs::write(path, &new_content)?;
    }

    Ok(true)
}

/// Bump version in a package.json file
pub fn bump_package_json(path: &PathBuf, new_version: &str, dry_run: bool) -> anyhow::Result<bool> {
    let content = std::fs::read_to_string(path)?;

    // Match "version": "..."
    let re = regex::Regex::new(r#"("version"\s*:\s*")([^"]+)(")"#)?;
    if !re.is_match(&content) {
        return Ok(false);
    }

    let old_version = re
        .captures(&content)
        .and_then(|c| c.get(2))
        .map(|m| m.as_str().to_string())
        .unwrap_or_default();

    if old_version == new_version {
        return Ok(false);
    }

    let new_content = re
        .replace(&content, format!("${{1}}{}${{3}}", new_version))
        .to_string();

    if !dry_run {
        std::fs::write(path, &new_content)?;
    }

    Ok(true)
}

/// Bump version in a file using a custom pattern from manifest config
pub fn bump_custom_file(
    path: &PathBuf,
    pattern: &str,
    new_version: &str,
    dry_run: bool,
) -> anyhow::Result<bool> {
    let content = std::fs::read_to_string(path)?;

    // The pattern uses {version} as a placeholder — escape regex-special chars
    // and replace {version} with a capture group
    let escaped = regex::escape(pattern);
    let regex_pattern = escaped.replace(r"\{version\}", r#"([^\s"']+)"#);
    let re = regex::Regex::new(&regex_pattern)?;

    if !re.is_match(&content) {
        return Ok(false);
    }

    let replacement = pattern.replace("{version}", new_version);
    let new_content = re.replace(&content, replacement.as_str()).to_string();

    if new_content == content {
        return Ok(false);
    }

    if !dry_run {
        std::fs::write(path, &new_content)?;
    }

    Ok(true)
}

/// Update CHANGELOG.md with a new version section
pub fn update_changelog(
    path: &PathBuf,
    version_tag: &str,
    notes: Option<&str>,
    dry_run: bool,
) -> anyhow::Result<bool> {
    if !path.exists() {
        return Ok(false);
    }

    let content = std::fs::read_to_string(path)?;
    let date = Local::now().format("%Y-%m-%d").to_string();

    let new_section = if let Some(notes) = notes {
        format!("## [{}] - {}\n\n{}\n\n", version_tag, date, notes)
    } else {
        format!("## [{}] - {}\n\n", version_tag, date)
    };

    // Insert after the first heading line (# Changelog or similar)
    let new_content = if let Some(pos) = content.find('\n') {
        // Check if there's a blank line after the heading
        let insert_pos = if content[pos + 1..].starts_with('\n') {
            pos + 2
        } else {
            pos + 1
        };
        format!(
            "{}\n{}{}",
            &content[..pos],
            new_section,
            &content[insert_pos..]
        )
    } else {
        format!("{}\n\n{}", content, new_section)
    };

    if !dry_run {
        std::fs::write(path, &new_content)?;
    }

    Ok(true)
}

/// Find the target repo for GitHub release creation
fn find_release_target<'a>(
    repos: &'a [RepoInfo],
    target_name: Option<&str>,
) -> anyhow::Result<&'a RepoInfo> {
    if let Some(name) = target_name {
        repos
            .iter()
            .find(|r| r.name == name)
            .ok_or_else(|| anyhow::anyhow!("Repository '{}' not found in manifest", name))
    } else {
        // Auto-detect: first non-reference repo
        repos
            .iter()
            .find(|r| !r.reference)
            .ok_or_else(|| anyhow::anyhow!("No non-reference repos found for release target"))
    }
}

/// Run the release command
pub async fn run_release(opts: ReleaseOptions<'_>) -> anyhow::Result<()> {
    let (bare_version, version_tag) = normalize_version(opts.version)?;
    let repos = filter_repos(opts.manifest, opts.workspace_root, None, None, false);

    let mut steps: Vec<StepResultJson> = Vec::new();

    if !opts.json {
        if opts.dry_run {
            Output::header(&format!("Release {} (dry run)", version_tag));
        } else {
            Output::header(&format!("Releasing {}", version_tag));
        }
        println!();
    }

    // ── Step 1: Bump version files ──────────────────────────────
    if !opts.json && !opts.quiet {
        Output::info(&format!("Step 1: Bumping version to {}", bare_version));
    }

    let release_config = opts
        .manifest
        .workspace
        .as_ref()
        .and_then(|w| w.release.as_ref());

    let mut bumped_files: Vec<String> = Vec::new();

    if let Some(config) = release_config.and_then(|r| r.version_files.as_ref()) {
        // Use manifest-configured version files
        for vf in config {
            let path = opts.workspace_root.join(&vf.path);
            if !path.exists() {
                if !opts.quiet {
                    Output::warning(&format!("Version file not found: {}", vf.path));
                }
                continue;
            }

            let changed = bump_custom_file(&path, &vf.pattern, &bare_version, opts.dry_run)?;
            if changed {
                bumped_files.push(vf.path.clone());
                if !opts.json && !opts.quiet {
                    Output::success(&format!("  Updated {}", vf.path));
                }
            }
        }
    } else {
        // Auto-detect version files
        let detected = detect_version_files(opts.workspace_root, &repos);
        for (repo_name, path) in &detected {
            let file_name = path.file_name().unwrap_or_default().to_string_lossy();
            let relative = path
                .strip_prefix(opts.workspace_root)
                .unwrap_or(path)
                .to_string_lossy()
                .to_string();

            let changed = if file_name == "Cargo.toml" {
                bump_cargo_toml(path, &bare_version, opts.dry_run)?
            } else if file_name == "package.json" {
                bump_package_json(path, &bare_version, opts.dry_run)?
            } else {
                false
            };

            if changed {
                bumped_files.push(relative.clone());
                if !opts.json && !opts.quiet {
                    Output::success(&format!("  Updated {} ({})", relative, repo_name));
                }
            }
        }
    }

    if bumped_files.is_empty() && !opts.quiet {
        Output::warning("  No version files were updated");
    }

    let mut step = StepResultJson::ok("bump-version");
    step.files = Some(bumped_files.clone());
    steps.push(step);

    // Update Cargo.lock if any Cargo.toml was bumped
    if !opts.dry_run {
        for file in &bumped_files {
            if file.ends_with("Cargo.toml") {
                let cargo_dir = opts.workspace_root.join(file);
                let cargo_dir = cargo_dir.parent().unwrap_or(opts.workspace_root);
                if cargo_dir.join("Cargo.lock").exists() {
                    let status = Command::new("cargo")
                        .arg("generate-lockfile")
                        .current_dir(cargo_dir)
                        .status();
                    if let Ok(s) = status {
                        if s.success() && !opts.json && !opts.quiet {
                            Output::success(&format!(
                                "  Updated Cargo.lock in {}",
                                cargo_dir.display()
                            ));
                        }
                    }
                }
            }
        }
    }

    // ── Step 2: Update CHANGELOG ────────────────────────────────
    if !opts.json && !opts.quiet {
        Output::info("Step 2: Updating CHANGELOG");
    }

    let changelog_path = release_config
        .and_then(|r| r.changelog.as_ref())
        .map(|p| opts.workspace_root.join(p))
        .unwrap_or_else(|| opts.workspace_root.join("CHANGELOG.md"));

    let changelog_updated =
        update_changelog(&changelog_path, &version_tag, opts.notes, opts.dry_run)?;

    if changelog_updated {
        let relative = changelog_path
            .strip_prefix(opts.workspace_root)
            .unwrap_or(&changelog_path)
            .to_string_lossy()
            .to_string();
        if !opts.json && !opts.quiet {
            Output::success(&format!("  Updated {}", relative));
        }
        steps.push(StepResultJson::ok("changelog"));
    } else {
        if !opts.json && !opts.quiet {
            Output::info("  No CHANGELOG.md found, skipping");
        }
        steps.push(StepResultJson::skipped("changelog"));
    }

    // ── Step 3: Build ───────────────────────────────────────────
    if !opts.json && !opts.quiet {
        Output::info("Step 3: Building");
    }

    let mut built_any = false;
    for repo in &repos {
        let build_cmd = repo.agent.as_ref().and_then(|a| a.build.as_deref());
        let Some(cmd) = build_cmd else {
            continue;
        };

        if opts.dry_run {
            if !opts.json && !opts.quiet {
                Output::info(&format!("  Would run in {}: {}", repo.name, cmd));
            }
            built_any = true;
            continue;
        }

        if !opts.json && !opts.quiet {
            Output::info(&format!("  Building {} ({})", repo.name, cmd));
        }

        let status = Command::new("sh")
            .arg("-c")
            .arg(cmd)
            .current_dir(&repo.absolute_path)
            .status()?;

        if !status.success() {
            anyhow::bail!(
                "Build failed for '{}' (exit code: {:?})",
                repo.name,
                status.code()
            );
        }

        built_any = true;
        if !opts.json && !opts.quiet {
            Output::success(&format!("  {} built successfully", repo.name));
        }
    }

    if built_any {
        steps.push(StepResultJson::ok("build"));
    } else {
        if !opts.json && !opts.quiet {
            Output::info("  No agent.build configured, skipping");
        }
        steps.push(StepResultJson::skipped("build"));
    }

    // ── Steps 4-9: PR workflow (skip if --skip-pr) ──────────────
    if opts.skip_pr {
        if !opts.json && !opts.quiet {
            Output::info("Skipping PR workflow (--skip-pr)");
        }
        steps.push(StepResultJson::skipped("branch"));
        steps.push(StepResultJson::skipped("pr"));
        steps.push(StepResultJson::skipped("ci"));
        steps.push(StepResultJson::skipped("merge"));
    } else {
        // Step 4: Create branch
        if !opts.json && !opts.quiet {
            Output::info(&format!("Step 4: Creating branch release/{}", version_tag));
        }

        if opts.dry_run {
            if !opts.json && !opts.quiet {
                Output::info(&format!(
                    "  Would create branch release/{} across repos",
                    version_tag
                ));
            }
            steps.push(StepResultJson::ok("branch"));
        } else {
            crate::cli::commands::branch::run_branch(
                crate::cli::commands::branch::BranchOptions {
                    workspace_root: opts.workspace_root,
                    manifest: opts.manifest,
                    name: Some(&format!("release/{}", version_tag)),
                    delete: false,
                    move_commits: false,
                    repos_filter: None,
                    group_filter: None,
                    json: opts.json,
                },
            )?;
            steps.push(StepResultJson::ok("branch"));
        }

        // Step 5: Stage all changes
        if !opts.json && !opts.quiet {
            Output::info("Step 5: Staging and committing changes");
        }

        if opts.dry_run {
            if !opts.json && !opts.quiet {
                Output::info(&format!(
                    "  Would commit: \"chore: release {}\"",
                    version_tag
                ));
            }
        } else {
            // Stage all changes
            crate::cli::commands::add::run_add(
                opts.workspace_root,
                opts.manifest,
                &[".".to_string()],
            )?;

            // Commit
            crate::cli::commands::commit::run_commit(
                opts.workspace_root,
                opts.manifest,
                &format!("chore: release {}", version_tag),
                false,
                opts.json,
            )?;
        }

        // Step 6: Push
        if !opts.json && !opts.quiet {
            Output::info("Step 6: Pushing to remote");
        }

        if opts.dry_run {
            if !opts.json && !opts.quiet {
                Output::info("  Would push with upstream tracking");
            }
        } else {
            crate::cli::commands::push::run_push(
                opts.workspace_root,
                opts.manifest,
                true,  // set_upstream
                false, // force
                opts.quiet,
                opts.json,
            )?;
        }

        // Step 7: Create PR
        if !opts.json && !opts.quiet {
            Output::info("Step 7: Creating pull request");
        }

        if opts.dry_run {
            if !opts.json && !opts.quiet {
                Output::info(&format!(
                    "  Would create PR: \"chore: release {}\"",
                    version_tag
                ));
            }
            steps.push(StepResultJson::ok("pr"));
        } else {
            crate::cli::commands::pr::run_pr_create(
                opts.workspace_root,
                opts.manifest,
                Some(&format!("chore: release {}", version_tag)),
                opts.notes,
                false, // draft
                false, // push (already pushed)
                false, // dry_run
                opts.json,
            )
            .await?;
            steps.push(StepResultJson::ok("pr"));
        }

        // Step 8: Wait for CI + Merge
        if !opts.json && !opts.quiet {
            Output::info("Step 8: Waiting for CI and merging");
        }

        if opts.dry_run {
            if !opts.json && !opts.quiet {
                Output::info(&format!(
                    "  Would wait {}s for CI, then merge",
                    opts.timeout
                ));
            }
            steps.push(StepResultJson::ok("ci"));
            steps.push(StepResultJson::ok("merge"));
        } else {
            crate::cli::commands::pr::run_pr_merge(
                opts.workspace_root,
                opts.manifest,
                None,  // method (default)
                false, // force
                false, // update
                false, // auto
                opts.json,
                true, // wait
                opts.timeout,
            )
            .await?;
            steps.push(StepResultJson::ok("ci"));
            steps.push(StepResultJson::ok("merge"));
        }

        // Step 9: Sync after merge
        if !opts.json && !opts.quiet {
            Output::info("Step 9: Syncing after merge");
        }

        if !opts.dry_run {
            // Checkout default branch
            let default_branch = repos
                .first()
                .map(|r| r.default_branch.as_str())
                .unwrap_or("main");
            crate::cli::commands::checkout::run_checkout(
                opts.workspace_root,
                opts.manifest,
                default_branch,
                false,
            )?;

            // Sync
            crate::cli::commands::sync::run_sync(
                opts.workspace_root,
                opts.manifest,
                false, // force
                opts.quiet,
                None,  // group
                false, // sequential
                false, // reset_refs
                opts.json,
                false, // no_hooks
            )
            .await?;
        }
    }

    // ── Step 10: Create GitHub release ──────────────────────────
    if !opts.json && !opts.quiet {
        Output::info("Step 10: Creating GitHub release");
    }

    let target_repo = find_release_target(&repos, opts.target_repo)?;

    if opts.dry_run {
        if !opts.json && !opts.quiet {
            Output::info(&format!(
                "  Would create release {} on {}/{} ({})",
                version_tag, target_repo.owner, target_repo.repo, target_repo.name
            ));
        }
        let mut step = StepResultJson::ok("release");
        step.url = Some(format!(
            "https://github.com/{}/{}/releases/tag/{}",
            target_repo.owner, target_repo.repo, version_tag
        ));
        steps.push(step);
    } else {
        let platform = get_platform_adapter(
            target_repo.platform_type,
            target_repo.platform_base_url.as_deref(),
        );

        let default_branch = &target_repo.default_branch;

        let result: ReleaseResult = platform
            .create_release(
                &target_repo.owner,
                &target_repo.repo,
                &version_tag,
                &version_tag,
                opts.notes,
                default_branch,
                false, // draft
                false, // prerelease
            )
            .await
            .map_err(|e| anyhow::anyhow!("Failed to create release: {}", e))?;

        if !opts.json && !opts.quiet {
            Output::success(&format!("  Created release: {}", result.url));
        }

        let mut step = StepResultJson::ok("release");
        step.url = Some(result.url);
        steps.push(step);
    }

    // ── Step 11: Post-release hooks ─────────────────────────────
    if let Some(hooks) = release_config.and_then(|r| r.post_release.as_ref()) {
        if !opts.json && !opts.quiet {
            Output::info("Step 11: Running post-release hooks");
        }

        for hook in hooks {
            let cmd = hook.command.replace("{version}", &bare_version);
            let display_name = hook.name.as_deref().unwrap_or(&cmd);

            if opts.dry_run {
                if !opts.json && !opts.quiet {
                    Output::info(&format!("  Would run: {}", display_name));
                }
                continue;
            }

            if !opts.json && !opts.quiet {
                Output::info(&format!("  Running: {}", display_name));
            }

            let working_dir = hook
                .cwd
                .as_ref()
                .map(|p| opts.workspace_root.join(p))
                .unwrap_or_else(|| opts.workspace_root.clone());

            let status = Command::new("sh")
                .arg("-c")
                .arg(&cmd)
                .current_dir(&working_dir)
                .status()?;

            if !status.success() {
                anyhow::bail!(
                    "Post-release hook '{}' failed (exit code: {:?})",
                    display_name,
                    status.code()
                );
            }

            if !opts.json && !opts.quiet {
                Output::success(&format!("  {} completed", display_name));
            }
        }
        steps.push(StepResultJson::ok("post-release"));
    }

    // ── Output ──────────────────────────────────────────────────
    if opts.json {
        let output = ReleaseOutputJson {
            version: bare_version,
            steps,
        };
        println!("{}", serde_json::to_string_pretty(&output)?);
    } else if !opts.quiet {
        println!();
        if opts.dry_run {
            Output::success(&format!("Dry run complete for {}", version_tag));
        } else {
            Output::success(&format!("Released {}", version_tag));
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_version_with_v_prefix() {
        let (bare, tag) = normalize_version("v1.2.3").unwrap();
        assert_eq!(bare, "1.2.3");
        assert_eq!(tag, "v1.2.3");
    }

    #[test]
    fn test_normalize_version_without_prefix() {
        let (bare, tag) = normalize_version("1.2.3").unwrap();
        assert_eq!(bare, "1.2.3");
        assert_eq!(tag, "v1.2.3");
    }

    #[test]
    fn test_normalize_version_two_parts() {
        let (bare, tag) = normalize_version("1.0").unwrap();
        assert_eq!(bare, "1.0");
        assert_eq!(tag, "v1.0");
    }

    #[test]
    fn test_normalize_version_prerelease() {
        let (bare, tag) = normalize_version("v1.2.3-rc.1").unwrap();
        assert_eq!(bare, "1.2.3-rc.1");
        assert_eq!(tag, "v1.2.3-rc.1");
    }

    #[test]
    fn test_normalize_version_invalid() {
        assert!(normalize_version("abc").is_err());
        assert!(normalize_version("x.y.z").is_err());
    }

    #[test]
    fn test_bump_cargo_toml() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Cargo.toml");
        std::fs::write(
            &path,
            r#"[package]
name = "test"
version = "0.1.0"
edition = "2021"
"#,
        )
        .unwrap();

        let changed = bump_cargo_toml(&path, "0.2.0", false).unwrap();
        assert!(changed);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains(r#"version = "0.2.0""#));
        assert!(!content.contains(r#"version = "0.1.0""#));
    }

    #[test]
    fn test_bump_cargo_toml_dry_run() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Cargo.toml");
        std::fs::write(
            &path,
            r#"[package]
name = "test"
version = "0.1.0"
"#,
        )
        .unwrap();

        let changed = bump_cargo_toml(&path, "0.2.0", true).unwrap();
        assert!(changed);

        // File should NOT be modified in dry run
        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains(r#"version = "0.1.0""#));
    }

    #[test]
    fn test_bump_cargo_toml_same_version() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("Cargo.toml");
        std::fs::write(
            &path,
            r#"[package]
name = "test"
version = "0.2.0"
"#,
        )
        .unwrap();

        let changed = bump_cargo_toml(&path, "0.2.0", false).unwrap();
        assert!(!changed);
    }

    #[test]
    fn test_bump_package_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("package.json");
        std::fs::write(
            &path,
            r#"{
  "name": "test",
  "version": "1.0.0"
}"#,
        )
        .unwrap();

        let changed = bump_package_json(&path, "1.1.0", false).unwrap();
        assert!(changed);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains(r#""version": "1.1.0""#));
    }

    #[test]
    fn test_bump_custom_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("version.txt");
        std::fs::write(&path, "APP_VERSION=1.0.0\n").unwrap();

        let changed = bump_custom_file(&path, "APP_VERSION={version}", "2.0.0", false).unwrap();
        assert!(changed);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains("APP_VERSION=2.0.0"));
    }

    #[test]
    fn test_update_changelog() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("CHANGELOG.md");
        std::fs::write(
            &path,
            "# Changelog\n\n## [v0.1.0] - 2025-01-01\n\n- Initial release\n",
        )
        .unwrap();

        let updated = update_changelog(&path, "v0.2.0", Some("New features"), false).unwrap();
        assert!(updated);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains("## [v0.2.0]"));
        assert!(content.contains("New features"));
        // Old entry should still be there
        assert!(content.contains("## [v0.1.0]"));
    }

    #[test]
    fn test_update_changelog_no_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("CHANGELOG.md");

        let updated = update_changelog(&path, "v0.1.0", None, false).unwrap();
        assert!(!updated);
    }

    #[test]
    fn test_update_changelog_without_notes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("CHANGELOG.md");
        std::fs::write(&path, "# Changelog\n\n## [v0.1.0] - 2025-01-01\n").unwrap();

        let updated = update_changelog(&path, "v0.2.0", None, false).unwrap();
        assert!(updated);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains("## [v0.2.0]"));
        assert!(content.contains("## [v0.1.0]"));
    }

    #[test]
    fn test_update_changelog_dry_run() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("CHANGELOG.md");
        std::fs::write(&path, "# Changelog\n\n## [v0.1.0] - 2025-01-01\n").unwrap();

        let updated = update_changelog(&path, "v0.2.0", Some("Changes"), true).unwrap();
        assert!(updated);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(!content.contains("v0.2.0")); // Should NOT be written in dry run
    }

    #[test]
    fn test_bump_package_json_same_version() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("package.json");
        std::fs::write(&path, r#"{"name": "test", "version": "1.0.0"}"#).unwrap();

        let changed = bump_package_json(&path, "1.0.0", false).unwrap();
        assert!(!changed);
    }

    #[test]
    fn test_bump_package_json_no_version_field() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("package.json");
        std::fs::write(&path, r#"{"name": "test"}"#).unwrap();

        let changed = bump_package_json(&path, "1.0.0", false).unwrap();
        assert!(!changed);
    }

    #[test]
    fn test_bump_custom_file_same_version() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("version.txt");
        std::fs::write(&path, "APP_VERSION=2.0.0\n").unwrap();

        let changed = bump_custom_file(&path, "APP_VERSION={version}", "2.0.0", false).unwrap();
        assert!(!changed);
    }

    #[test]
    fn test_bump_custom_file_no_match() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("version.txt");
        std::fs::write(&path, "OTHER_KEY=1.0.0\n").unwrap();

        let changed = bump_custom_file(&path, "APP_VERSION={version}", "2.0.0", false).unwrap();
        assert!(!changed);
    }

    #[test]
    fn test_bump_custom_file_dry_run() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("version.txt");
        std::fs::write(&path, "APP_VERSION=1.0.0\n").unwrap();

        let changed = bump_custom_file(&path, "APP_VERSION={version}", "2.0.0", true).unwrap();
        assert!(changed);

        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.contains("1.0.0")); // Should NOT be written in dry run
    }

    #[test]
    fn test_find_release_target_explicit() {
        let repos = vec![
            RepoInfo {
                name: "frontend".to_string(),
                url: "https://github.com/test/frontend.git".to_string(),
                path: "./frontend".to_string(),
                absolute_path: PathBuf::from("/tmp/frontend"),
                default_branch: "main".to_string(),
                owner: "test".to_string(),
                repo: "frontend".to_string(),
                platform_type: crate::core::manifest::PlatformType::GitHub,
                platform_base_url: None,
                project: None,
                reference: false,
                groups: vec![],
                agent: None,
            },
            RepoInfo {
                name: "backend".to_string(),
                url: "https://github.com/test/backend.git".to_string(),
                path: "./backend".to_string(),
                absolute_path: PathBuf::from("/tmp/backend"),
                default_branch: "main".to_string(),
                owner: "test".to_string(),
                repo: "backend".to_string(),
                platform_type: crate::core::manifest::PlatformType::GitHub,
                platform_base_url: None,
                project: None,
                reference: false,
                groups: vec![],
                agent: None,
            },
        ];

        let target = find_release_target(&repos, Some("backend")).unwrap();
        assert_eq!(target.name, "backend");
    }

    #[test]
    fn test_find_release_target_auto_detect() {
        let repos = vec![
            RepoInfo {
                name: "ref-repo".to_string(),
                url: "https://github.com/test/ref.git".to_string(),
                path: "./ref".to_string(),
                absolute_path: PathBuf::from("/tmp/ref"),
                default_branch: "main".to_string(),
                owner: "test".to_string(),
                repo: "ref".to_string(),
                platform_type: crate::core::manifest::PlatformType::GitHub,
                platform_base_url: None,
                project: None,
                reference: true,
                groups: vec![],
                agent: None,
            },
            RepoInfo {
                name: "main-repo".to_string(),
                url: "https://github.com/test/main.git".to_string(),
                path: "./main".to_string(),
                absolute_path: PathBuf::from("/tmp/main"),
                default_branch: "main".to_string(),
                owner: "test".to_string(),
                repo: "main".to_string(),
                platform_type: crate::core::manifest::PlatformType::GitHub,
                platform_base_url: None,
                project: None,
                reference: false,
                groups: vec![],
                agent: None,
            },
        ];

        let target = find_release_target(&repos, None).unwrap();
        assert_eq!(target.name, "main-repo"); // Skips reference repos
    }

    #[test]
    fn test_find_release_target_not_found() {
        let repos = vec![];
        assert!(find_release_target(&repos, Some("missing")).is_err());
        assert!(find_release_target(&repos, None).is_err());
    }

    #[test]
    fn test_detect_version_files() {
        let dir = tempfile::tempdir().unwrap();
        let repo_dir = dir.path().join("my-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(
            repo_dir.join("Cargo.toml"),
            "[package]\nname = \"test\"\nversion = \"0.1.0\"\n",
        )
        .unwrap();

        let repos = vec![RepoInfo {
            name: "my-repo".to_string(),
            url: "https://github.com/test/my-repo.git".to_string(),
            path: "./my-repo".to_string(),
            absolute_path: repo_dir,
            default_branch: "main".to_string(),
            owner: "test".to_string(),
            repo: "my-repo".to_string(),
            platform_type: crate::core::manifest::PlatformType::GitHub,
            platform_base_url: None,
            project: None,
            reference: false,
            groups: vec![],
            agent: None,
        }];

        let files = detect_version_files(&dir.path().to_path_buf(), &repos);
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].0, "my-repo");
        assert!(files[0].1.ends_with("Cargo.toml"));
    }

    #[test]
    fn test_detect_version_files_skips_reference() {
        let dir = tempfile::tempdir().unwrap();
        let repo_dir = dir.path().join("ref-repo");
        std::fs::create_dir_all(&repo_dir).unwrap();
        std::fs::write(repo_dir.join("Cargo.toml"), "[package]\n").unwrap();

        let repos = vec![RepoInfo {
            name: "ref-repo".to_string(),
            url: "https://github.com/test/ref.git".to_string(),
            path: "./ref".to_string(),
            absolute_path: repo_dir,
            default_branch: "main".to_string(),
            owner: "test".to_string(),
            repo: "ref".to_string(),
            platform_type: crate::core::manifest::PlatformType::GitHub,
            platform_base_url: None,
            project: None,
            reference: true,
            groups: vec![],
            agent: None,
        }];

        let files = detect_version_files(&dir.path().to_path_buf(), &repos);
        assert!(files.is_empty());
    }
}

//! Additional edge case tests for improved coverage.
//!
//! Covers: group-filtered status, CI with workspace env vars,
//! Bitbucket failure paths, diff staged mode, and more.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;
use std::fs;

// ── Status with group filter ──────────────────────────────────────

#[test]
fn test_status_group_filter() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("frontend", vec!["ui"])
        .add_repo_with_groups("backend", vec!["api"])
        .add_repo_with_groups("shared", vec!["ui", "api"])
        .build();

    let manifest = ws.load_manifest();
    let group = vec!["ui".to_string()];

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        Some(&group),
        false,
    );
    assert!(
        result.is_ok(),
        "status with group filter should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_status_group_filter_json() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("frontend", vec!["ui"])
        .add_repo_with_groups("backend", vec!["api"])
        .build();

    let manifest = ws.load_manifest();
    let group = vec!["api".to_string()];

    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        Some(&group),
        true, // json
    );
    assert!(
        result.is_ok(),
        "status json with group filter should succeed: {:?}",
        result.err()
    );
}

// ── Diff staged mode ──────────────────────────────────────────────

#[test]
fn test_diff_staged_no_changes() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        true, // staged
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff staged with no changes should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_diff_staged_json() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::diff::run_diff(
        &ws.workspace_root,
        &manifest,
        true, // staged
        true, // json
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "diff staged json should succeed: {:?}",
        result.err()
    );
}

// ── Branch with group filter (JSON) ──────────────────────────────

#[test]
fn test_branch_group_filter_json() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("frontend", vec!["ui"])
        .add_repo_with_groups("backend", vec!["api"])
        .build();

    let manifest = ws.load_manifest();
    let group = vec!["ui".to_string()];

    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: None,
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: Some(&group),
            json: true,
        });
    assert!(
        result.is_ok(),
        "branch json with group filter should succeed: {:?}",
        result.err()
    );
}

// ── CI with workspace env vars ──────────────────────────────────

fn write_ci_env_manifest(ws: &common::fixtures::WorkspaceFixture, yaml: &str) {
    let manifest_path =
        gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(&ws.workspace_root)
            .expect("workspace manifest path should resolve");
    let existing = fs::read_to_string(&manifest_path).unwrap();
    let full = format!("{}\nworkspace:\n{}", existing, yaml);
    fs::write(&manifest_path, full).unwrap();
}

#[test]
fn test_ci_run_with_workspace_env() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    write_ci_env_manifest(
        &ws,
        r#"  env:
    MY_VAR: hello
  ci:
    pipelines:
      env-test:
        steps:
          - name: check-env
            command: "echo $MY_VAR"
"#,
    );

    let manifest = ws.load_manifest();
    let result =
        gitgrip::cli::commands::ci::run_ci_run(&ws.workspace_root, &manifest, "env-test", false);
    assert!(
        result.is_ok(),
        "CI with workspace env should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_ci_run_step_with_env_override() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    write_ci_env_manifest(
        &ws,
        r#"  ci:
    pipelines:
      env-override:
        steps:
          - name: with-env
            command: "echo $STEP_VAR"
            env:
              STEP_VAR: step-value
"#,
    );

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::ci::run_ci_run(
        &ws.workspace_root,
        &manifest,
        "env-override",
        false,
    );
    assert!(
        result.is_ok(),
        "CI with step env override should succeed: {:?}",
        result.err()
    );
}

// ── Bitbucket URL parsing edge cases ──────────────────────────────

#[test]
fn test_bb_matches_enterprise_url() {
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(None);
    use gitgrip::platform::traits::HostingPlatform;
    assert!(adapter.matches_url("git@bitbucket.mycompany.com:team/repo.git"));
    assert!(adapter.matches_url("https://bitbucket.mycompany.com/team/repo"));
    assert!(!adapter.matches_url("git@github.com:owner/repo.git"));
    assert!(!adapter.matches_url("https://gitlab.com/user/repo"));
}

#[test]
fn test_bb_parse_enterprise_url() {
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(None);
    use gitgrip::platform::traits::HostingPlatform;

    let info = adapter.parse_repo_url("git@bitbucket.mycompany.com:team/enterprise-repo.git");
    assert!(info.is_some(), "should parse enterprise Bitbucket SSH URL");
    let info = info.unwrap();
    assert_eq!(info.owner, "team");
    assert_eq!(info.repo, "enterprise-repo");
}

#[test]
fn test_bb_parse_invalid_url_returns_none() {
    let adapter = gitgrip::platform::bitbucket::BitbucketAdapter::new(None);
    use gitgrip::platform::traits::HostingPlatform;

    assert!(
        adapter
            .parse_repo_url("git@github.com:owner/repo.git")
            .is_none(),
        "should not parse GitHub URL as Bitbucket"
    );
    assert!(
        adapter.parse_repo_url("not-a-url").is_none(),
        "should not parse invalid URL"
    );
}

// ── Rebase continue with no rebase ──────────────────────────────

#[test]
fn test_rebase_continue_no_rebase() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Continue with no rebase in progress → should succeed (no-op)
    let result = gitgrip::cli::commands::rebase::run_rebase(
        &ws.workspace_root,
        &manifest,
        None,
        false,
        false,
        true, // continue
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "continue with no rebase should succeed: {:?}",
        result.err()
    );
}

// ── Checkout back to main ──────────────────────────────────────

#[test]
fn test_checkout_main_from_feature() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create feature branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/roundtrip"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Checkout main
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout main should succeed: {:?}",
        result.err()
    );

    common::assertions::assert_on_branch(&ws.repo_path("frontend"), "main");
    common::assertions::assert_on_branch(&ws.repo_path("backend"), "main");
}

// ── GC with reference repos ──────────────────────────────────────

#[test]
fn test_gc_with_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::gc::run_gc(&ws.workspace_root, &manifest, false, false, None, None);
    assert!(
        result.is_ok(),
        "gc with reference repos should succeed: {:?}",
        result.err()
    );
}

// ── Commit amend flag ──────────────────────────────────────────

#[test]
fn test_commit_amend_with_changes() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Create a new file and stage it
    fs::write(ws.repo_path("app").join("new.txt"), "content").unwrap();
    git_helpers::commit_file(&ws.repo_path("app"), "new.txt", "content", "Initial");

    // Modify and stage
    fs::write(ws.repo_path("app").join("new.txt"), "amended content").unwrap();
    std::process::Command::new("git")
        .args(["add", "new.txt"])
        .current_dir(ws.repo_path("app"))
        .output()
        .unwrap();

    let result = gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "amended message",
        true, // amend
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "commit amend should succeed: {:?}",
        result.err()
    );
}

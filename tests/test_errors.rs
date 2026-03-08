//! Error scenario tests for gitgrip.
//!
//! Tests that various error conditions are handled gracefully:
//! - Invalid manifests
//! - Missing/broken repos
//! - Operations on non-existent branches
//! - Workspace boundary violations

mod common;

use tempfile::TempDir;

use gitgrip::core::griptree::GriptreeConfig;

use common::fixtures::WorkspaceBuilder;

// ── Invalid Manifest ──────────────────────────────────────────────

#[test]
fn test_invalid_yaml_manifest() {
    let result = gitgrip::core::manifest::Manifest::parse("{{{{not yaml");
    assert!(result.is_err(), "should fail on invalid YAML");
}

#[test]
fn test_empty_repos_manifest() {
    let yaml = "version: 1\nrepos:\n";
    let result = gitgrip::core::manifest::Manifest::parse(yaml);
    assert!(result.is_err(), "should fail with empty repos");
}

#[test]
fn test_manifest_missing_url() {
    let yaml = r#"
version: 1
repos:
  myrepo:
    path: myrepo
    default_branch: main
"#;
    let result = gitgrip::core::manifest::Manifest::parse(yaml);
    // Should either fail to parse or fail validation (URL is required)
    assert!(
        result.is_err(),
        "should fail when repo is missing URL field"
    );
}

#[test]
fn test_manifest_path_traversal() {
    let yaml = r#"
version: 1
repos:
  evil:
    url: https://github.com/test/repo.git
    path: ../../etc/passwd
    default_branch: main
"#;
    let result = gitgrip::core::manifest::Manifest::parse(yaml);
    assert!(result.is_err(), "should reject path traversal");
    let err = result.unwrap_err().to_string();
    assert!(
        err.contains("Path escapes") || err.contains("traversal") || err.contains("boundary"),
        "error should mention path escaping: {}",
        err
    );
}

// ── Invalid Griptree Config ───────────────────────────────────────

#[test]
fn test_invalid_griptree_config() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("workspace");
    std::fs::create_dir_all(workspace_root.join(".gitgrip")).unwrap();
    std::fs::write(
        workspace_root.join(".gitgrip").join("griptree.json"),
        "{invalid json",
    )
    .unwrap();

    let result = gitgrip::core::griptree::GriptreeConfig::load_from_workspace(&workspace_root);
    assert!(result.is_err(), "invalid griptree config should error");
}

#[test]
fn test_invalid_griptree_upstream_format() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("workspace");
    std::fs::create_dir_all(workspace_root.join(".gitgrip")).unwrap();

    let mut config = GriptreeConfig::new("feat/griptree", "/workspace");
    config
        .repo_upstreams
        .insert("app".to_string(), "main".to_string());
    config
        .save(&workspace_root.join(".gitgrip").join("griptree.json"))
        .unwrap();

    let config = GriptreeConfig::load_from_workspace(&workspace_root)
        .unwrap()
        .unwrap();
    let result = config.upstream_for_repo("app", "main");
    assert!(result.is_err(), "invalid upstream should return an error");
}

// ── Missing/Broken Repos ──────────────────────────────────────────

#[test]
fn test_status_with_missing_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("broken")
        .build();

    // Delete the "broken" repo to simulate a missing clone
    std::fs::remove_dir_all(ws.repo_path("broken")).unwrap();

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::status::run_status(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
    );
    // Status should succeed even with a missing repo (reports "not cloned")
    assert!(
        result.is_ok(),
        "status should handle missing repos gracefully: {:?}",
        result.err()
    );
}

#[test]
fn test_commit_with_no_staged_changes() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Try to commit with nothing staged
    let result = gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "should not commit",
        false,
        false,
        None,
        None,
    );
    // Should succeed but report "no changes to commit"
    assert!(
        result.is_ok(),
        "commit with no changes should not error: {:?}",
        result.err()
    );
}

#[test]
fn test_checkout_nonexistent_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "nonexistent-branch",
        false,
        None,
        None,
    );
    // Should succeed (returns Ok) but skip repos where branch doesn't exist
    assert!(
        result.is_ok(),
        "checkout nonexistent branch should not crash: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_already_exists() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Create branch first time
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/exists"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Switch back to main
    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();

    // Try creating same branch again - should handle gracefully
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/exists"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            json: false,
        });
    assert!(
        result.is_ok(),
        "creating existing branch should not crash: {:?}",
        result.err()
    );
}

// ── Sync with Broken Remote ──────────────────────────────────────

#[tokio::test]
async fn test_sync_with_deleted_remote() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Delete the bare remote to simulate inaccessible remote
    std::fs::remove_dir_all(ws.remote_path("app")).unwrap();

    let manifest = ws.load_manifest();
    // Sync should handle missing remote gracefully (error or report per-repo failure)
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
        false,
        false,
        false,
    )
    .await;
    // Whether it returns Ok (with per-repo error reports) or Err is acceptable,
    // but it must not panic. Verify we got a determinate result.
    match &result {
        Ok(_) => {} // Graceful handling with per-repo error reports
        Err(e) => {
            let msg = e.to_string();
            assert!(
                !msg.is_empty(),
                "sync error should have a descriptive message"
            );
        }
    }
}

// ── Push Without Remote Branch ──────────────────────────────────

#[test]
fn test_push_on_main_nothing_to_push() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Push on main with no new commits - should succeed silently
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "push with nothing should succeed: {:?}",
        result.err()
    );
}

// ── Forall with Failing Command ──────────────────────────────────

#[test]
fn test_forall_with_nonexistent_command() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();
    let manifest = ws.load_manifest();

    // Run a command that will fail in all repos
    let result = gitgrip::cli::commands::forall::run_forall(
        &ws.workspace_root,
        &manifest,
        "nonexistent-command-that-doesnt-exist-12345",
        false, // parallel
        false, // changed_only
        false, // no_intercept
        None,
        None,
    );
    // Forall should handle per-repo command failures gracefully
    match &result {
        Ok(_) => {} // Graceful handling with per-repo failure reports
        Err(e) => {
            let msg = e.to_string();
            assert!(
                !msg.is_empty(),
                "forall error should have a descriptive message"
            );
        }
    }
}

// ── Add with No Changes ──────────────────────────────────────────

#[test]
fn test_add_with_no_changes() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Add when there's nothing to add
    let files = vec![".".to_string()];
    let result =
        gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None);
    assert!(
        result.is_ok(),
        "add with no changes should succeed: {:?}",
        result.err()
    );
}

// ── Platform Detection Edge Cases ────────────────────────────────

#[test]
fn test_detect_platform_unknown_url() {
    let platform = gitgrip::platform::detect_platform("https://unknown-host.example.com/repo.git");
    // Should default to GitHub for unknown hosts
    assert_eq!(platform, gitgrip::core::manifest::PlatformType::GitHub);
}

#[test]
fn test_detect_platform_file_url() {
    let platform = gitgrip::platform::detect_platform("file:///tmp/repo.git");
    // File URLs should default to GitHub (no platform detection possible)
    assert_eq!(platform, gitgrip::core::manifest::PlatformType::GitHub);
}

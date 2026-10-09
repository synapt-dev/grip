//! Workflow integration tests
//!
//! Tests complete multi-repo workflows using temporary repositories.
//!
//! These tests require specific setup and are marked with #[ignore] by default.
//! Run with: cargo test --test workflow_tests -- --ignored
//!
//! TODO: Implement proper local workspace testing with mock remotes.

use std::fs;
use std::process::Command;
use tempfile::TempDir;

/// Helper to run git commands in a directory
fn git(dir: &std::path::Path, args: &[&str]) -> bool {
    Command::new("git")
        .current_dir(dir)
        .args(args)
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Create a test git repository with an initial commit
fn create_test_repo(dir: &std::path::Path) {
    git(dir, &["init", "-b", "main"]);
    git(dir, &["config", "user.email", "test@example.com"]);
    git(dir, &["config", "user.name", "Test User"]);

    fs::write(dir.join("README.md"), "# Test Repo\n").unwrap();
    git(dir, &["add", "."]);
    git(dir, &["commit", "-m", "Initial commit"]);
}

/// Create a workspace with manifest and repos
fn create_test_workspace(temp: &TempDir) -> std::path::PathBuf {
    let workspace = temp.path().to_path_buf();

    // Create .gitgrip/manifests directory
    let manifest_dir = workspace.join(".gitgrip").join("manifests");
    fs::create_dir_all(&manifest_dir).unwrap();

    // Create repo1
    let repo1 = workspace.join("repo1");
    fs::create_dir_all(&repo1).unwrap();
    create_test_repo(&repo1);

    // Create repo2
    let repo2 = workspace.join("repo2");
    fs::create_dir_all(&repo2).unwrap();
    create_test_repo(&repo2);

    // Create manifest with absolute paths (required for local testing)
    let manifest = format!(
        r#"version: 1
repos:
  repo1:
    url: file://{}
    path: repo1
    default_branch: main
  repo2:
    url: file://{}
    path: repo2
    default_branch: main
"#,
        repo1.display(),
        repo2.display()
    );
    fs::write(manifest_dir.join("manifest.yaml"), manifest).unwrap();

    workspace
}

/// Test that manifest parsing works correctly
#[test]
fn test_manifest_parsing() {
    use gitgrip::core::manifest::Manifest;

    let manifest = r#"
version: 1
repos:
  app:
    url: git@github.com:user/app.git
    path: app
    default_branch: main
  lib:
    url: git@github.com:user/lib.git
    path: lib
"#;

    let result = Manifest::parse(manifest);
    assert!(result.is_ok(), "Manifest should parse successfully");

    let manifest = result.unwrap();
    assert_eq!(manifest.repos.len(), 2, "Should have 2 repos");
    assert!(manifest.repos.contains_key("app"), "Should have app repo");
    assert!(manifest.repos.contains_key("lib"), "Should have lib repo");
}

/// Test state file parsing
#[test]
fn test_state_parsing() {
    use gitgrip::core::state::StateFile;

    let state_json = r#"{
        "currentManifestPr": 42,
        "branchToPr": {
            "feat/test": 42
        },
        "prLinks": {}
    }"#;

    let result = StateFile::parse(state_json);
    assert!(result.is_ok(), "State should parse successfully");

    let state = result.unwrap();
    assert_eq!(state.current_manifest_pr, Some(42));
    assert_eq!(state.get_pr_for_branch("feat/test"), Some(42));
}

/// Test creating a local workspace
/// This test requires local file:// URL support which may not work in all environments.
#[test]
#[ignore = "Requires local file:// URL support"]
fn test_local_workspace_status() {
    let temp = TempDir::new().unwrap();
    let workspace = create_test_workspace(&temp);

    let output = Command::new(env!("CARGO_BIN_EXE_gr1"))
        .current_dir(&workspace)
        .arg("status")
        .output()
        .expect("Failed to execute gr status");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    // Should show repos
    assert!(
        stdout.contains("repo1") || stderr.contains("repo"),
        "Should show repos in output. stdout: {}, stderr: {}",
        stdout,
        stderr
    );
}

/// Test branch creation in a local workspace
#[test]
#[ignore = "Requires local file:// URL support"]
fn test_local_branch_creation() {
    let temp = TempDir::new().unwrap();
    let workspace = create_test_workspace(&temp);

    let output = Command::new(env!("CARGO_BIN_EXE_gr1"))
        .current_dir(&workspace)
        .args(["branch", "feat/test-branch"])
        .output()
        .expect("Failed to execute gr branch");

    assert!(output.status.success(), "gr branch should succeed");
}

/// Test add and commit in a local workspace
#[test]
#[ignore = "Requires local file:// URL support"]
fn test_local_add_commit() {
    let temp = TempDir::new().unwrap();
    let workspace = create_test_workspace(&temp);

    // Create a branch
    let _ = Command::new(env!("CARGO_BIN_EXE_gr1"))
        .current_dir(&workspace)
        .args(["branch", "feat/changes"])
        .output();

    // Make changes
    fs::write(workspace.join("repo1").join("new.txt"), "new file").unwrap();

    // Add and commit
    let _ = Command::new(env!("CARGO_BIN_EXE_gr1"))
        .current_dir(&workspace)
        .args(["add", "."])
        .output();

    let output = Command::new(env!("CARGO_BIN_EXE_gr1"))
        .current_dir(&workspace)
        .args(["commit", "-m", "Add new file"])
        .output()
        .expect("Failed to commit");

    assert!(output.status.success(), "gr commit should succeed");
}

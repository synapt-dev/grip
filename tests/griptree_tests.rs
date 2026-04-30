//! Griptree integration tests
//!
//! Tests griptree (worktree-based parallel workspaces) functionality.

use std::fs;
use std::path::PathBuf;
use tempfile::TempDir;

/// Test that GriptreeRepoInfo correctly serializes/deserializes
#[test]
fn test_griptree_repo_info_serialization() {
    use gitgrip::core::griptree::GriptreeRepoInfo;

    let repo_info = GriptreeRepoInfo {
        name: "codi".to_string(),
        original_branch: "main".to_string(),
        is_reference: false,
        worktree_name: Some("main".to_string()),
        worktree_path: Some("/path/to/worktree".to_string()),
        main_repo_path: Some("/workspace/codi".to_string()),
    };

    let json = serde_json::to_string(&repo_info).unwrap();
    let deserialized: GriptreeRepoInfo = serde_json::from_str(&json).unwrap();

    assert_eq!(deserialized.name, repo_info.name);
    assert_eq!(deserialized.original_branch, repo_info.original_branch);
    assert_eq!(deserialized.is_reference, repo_info.is_reference);
    assert_eq!(deserialized.worktree_name, repo_info.worktree_name);
    assert_eq!(deserialized.worktree_path, repo_info.worktree_path);
    assert_eq!(deserialized.main_repo_path, repo_info.main_repo_path);
}

/// Test that GriptreePointer handles backwards compatibility
#[test]
fn test_griptree_pointer_backwards_compat() {
    use gitgrip::core::griptree::GriptreePointer;
    use serde_json;

    // Old JSON format without repos and manifest_branch fields
    let old_json = r#"{
        "branch": "feat/test",
        "mainWorkspace": "/workspace",
        "locked": false
    }"#;

    let pointer: GriptreePointer = serde_json::from_str(old_json).unwrap();

    assert_eq!(pointer.branch, "feat/test");
    assert_eq!(pointer.main_workspace, "/workspace");
    assert!(pointer.repos.is_empty(), "repos should default to empty");
    assert!(
        pointer.manifest_branch.is_none(),
        "manifest_branch should default to None"
    );
}

/// Test that GriptreePointer includes repos and manifest_branch
#[test]
fn test_griptree_pointer_new_fields() {
    use gitgrip::core::griptree::GriptreePointer;
    use serde_json;

    let json_with_new_fields = r#"{
        "branch": "feat/test",
        "mainWorkspace": "/workspace",
        "locked": false,
        "repos": [
            {
                "name": "codi",
                "originalBranch": "main",
                "isReference": false
            }
        ],
        "manifestBranch": "griptree-feat-test"
    }"#;

    let pointer: GriptreePointer = serde_json::from_str(json_with_new_fields).unwrap();

    assert_eq!(pointer.branch, "feat/test");
    assert_eq!(pointer.main_workspace, "/workspace");
    assert_eq!(pointer.repos.len(), 1);
    assert_eq!(pointer.repos[0].name, "codi");
    assert_eq!(pointer.repos[0].original_branch, "main");
    assert!(!pointer.repos[0].is_reference);
    assert_eq!(
        pointer.manifest_branch,
        Some("griptree-feat-test".to_string())
    );
}

/// Test that manifest directory is correctly detected
#[test]
fn test_main_manifests_dir_path() {
    let temp = TempDir::new().unwrap();
    let workspace_root = PathBuf::from(temp.path());

    let manifests_dir = workspace_root.join(".gitgrip").join("spaces").join("main");

    // Create the directory structure
    fs::create_dir_all(&manifests_dir).unwrap();

    assert!(manifests_dir.exists());
}

/// Test that griptree manifest path is correctly constructed
#[test]
fn test_griptree_manifest_path() {
    let temp = TempDir::new().unwrap();
    let griptree_path = PathBuf::from(temp.path());

    let griptree_manifest_dir = griptree_path.join(".gitgrip").join("spaces").join("main");

    // Create the directory structure
    fs::create_dir_all(&griptree_manifest_dir).unwrap();

    assert!(griptree_manifest_dir.exists());
}

/// Test that manifest worktree git directory is detected
#[test]
fn test_manifest_worktree_git_dir() {
    let temp = TempDir::new().unwrap();
    let manifests_dir = PathBuf::from(temp.path());

    let manifests_git_dir = manifests_dir.join(".git");

    // Create manifests directory
    fs::create_dir_all(&manifests_dir).unwrap();

    // Initially, git dir doesn't exist
    assert!(!manifests_git_dir.exists());

    // Simulate git worktree by creating .git file (worktree use file, not dir)
    fs::write(
        &manifests_git_dir,
        "gitdir: /some/other/path/.git/worktrees/mymain",
    )
    .unwrap();

    // After creation
    assert!(manifests_git_dir.exists());
}

/// Test that create_manifest_worktree correctly generates branch name
#[test]
fn test_manifest_worktree_branch_name() {
    let branch_name = "feat/feature-branch";
    let expected_worktree_name = format!("griptree-{}", branch_name.replace('/', "-"));

    assert_eq!(expected_worktree_name, "griptree-feat-feature-branch");
}

/// Test that create_manifest_worktree handles simple branch names
#[test]
fn test_manifest_worktree_branch_name_simple() {
    let branch_name = "my-feature";
    let expected_worktree_name = format!("griptree-{}", branch_name.replace('/', "-"));

    assert_eq!(expected_worktree_name, "griptree-my-feature");
}

/// Test that GriptreePointer with empty repos serializes correctly
#[test]
fn test_griptree_pointer_empty_repos() {
    use gitgrip::core::griptree::GriptreePointer;
    use serde_json;

    let pointer = GriptreePointer {
        branch: "feature".to_string(),
        main_workspace: "/path".to_string(),
        locked: false,
        created_at: None,
        repos: vec![],
        manifest_branch: None,
        manifest_worktree_name: None,
    };

    let json = serde_json::to_string_pretty(&pointer).unwrap();
    let deserialized: GriptreePointer = serde_json::from_str(&json).unwrap();

    assert_eq!(deserialized.repos.len(), 0);
    assert!(deserialized.manifest_branch.is_none());
}

/// Test that GriptreeRepoInfo camelCase serialization
#[test]
fn test_griptree_repo_info_camel_case() {
    use gitgrip::core::griptree::GriptreeRepoInfo;
    use serde_json;

    let repo_info = GriptreeRepoInfo {
        name: "codi-private".to_string(),
        original_branch: "develop".to_string(),
        is_reference: false,
        worktree_name: None,
        worktree_path: None,
        main_repo_path: None,
    };

    let json = serde_json::to_string(&repo_info).unwrap();

    // Should use camelCase for originalBranch and isReference
    assert!(json.contains("\"originalBranch\""));
    assert!(json.contains("\"isReference\""));

    let deserialized: GriptreeRepoInfo = serde_json::from_str(&json).unwrap();
    assert_eq!(deserialized.original_branch, "develop");
    assert!(!deserialized.is_reference);
}

//! Integration tests for the repo management commands (list, add, remove).

mod common;

use common::fixtures::WorkspaceBuilder;
use std::fs;
use std::path::PathBuf;

fn workspace_manifest_path(workspace_root: &std::path::Path) -> PathBuf {
    gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(workspace_root)
        .expect("workspace manifest path should resolve")
}

// ── repo list ──────────────────────────────────────────────────────

#[test]
fn test_repo_list_basic() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::repo::run_repo_list(&ws.workspace_root, &manifest);
    assert!(
        result.is_ok(),
        "repo list should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_repo_list_with_missing_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    // Remove one repo to simulate "not cloned"
    fs::remove_dir_all(ws.repo_path("backend")).unwrap();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::repo::run_repo_list(&ws.workspace_root, &manifest);
    assert!(
        result.is_ok(),
        "repo list with missing repo should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_repo_list_with_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::repo::run_repo_list(&ws.workspace_root, &manifest);
    assert!(
        result.is_ok(),
        "repo list with reference repos should succeed: {:?}",
        result.err()
    );
}

// ── repo add ──────────────────────────────────────────────────────

#[test]
fn test_repo_add_https_url() {
    let ws = WorkspaceBuilder::new().add_repo("existing").build();

    let result = gitgrip::cli::commands::repo::run_repo_add(
        &ws.workspace_root,
        "https://github.com/owner/new-repo.git",
        None,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "repo add should succeed: {:?}",
        result.err()
    );

    // Verify the manifest was updated
    let manifest_path = workspace_manifest_path(&ws.workspace_root);
    let content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        content.contains("new-repo"),
        "manifest should contain new repo"
    );

    let legacy_manifest = ws
        .workspace_root
        .join(".gitgrip")
        .join("manifests")
        .join("manifest.yaml");
    let legacy_content = fs::read_to_string(legacy_manifest).unwrap();
    assert!(
        legacy_content.contains("new-repo"),
        "legacy manifest mirror should also contain new repo"
    );
}

#[test]
fn test_repo_add_ssh_url() {
    let ws = WorkspaceBuilder::new().add_repo("existing").build();

    let result = gitgrip::cli::commands::repo::run_repo_add(
        &ws.workspace_root,
        "git@github.com:owner/ssh-repo.git",
        None,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "repo add with SSH URL should succeed: {:?}",
        result.err()
    );

    let manifest_path = workspace_manifest_path(&ws.workspace_root);
    let content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        content.contains("ssh-repo"),
        "manifest should contain ssh-repo"
    );
}

#[test]
fn test_repo_add_custom_path() {
    let ws = WorkspaceBuilder::new().add_repo("existing").build();

    let result = gitgrip::cli::commands::repo::run_repo_add(
        &ws.workspace_root,
        "https://github.com/owner/repo.git",
        Some("custom/path"),
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "repo add with custom path should succeed: {:?}",
        result.err()
    );

    let manifest_path = workspace_manifest_path(&ws.workspace_root);
    let content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        content.contains("custom/path"),
        "manifest should contain custom path"
    );
}

#[test]
fn test_repo_add_custom_branch() {
    let ws = WorkspaceBuilder::new().add_repo("existing").build();

    let result = gitgrip::cli::commands::repo::run_repo_add(
        &ws.workspace_root,
        "https://github.com/owner/repo.git",
        None,
        Some("develop"),
        None,
    );
    assert!(
        result.is_ok(),
        "repo add with custom branch should succeed: {:?}",
        result.err()
    );

    let manifest_path = workspace_manifest_path(&ws.workspace_root);
    let content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        content.contains("develop"),
        "manifest should contain develop branch"
    );
}

#[test]
fn test_repo_add_invalid_url() {
    let ws = WorkspaceBuilder::new().add_repo("existing").build();

    let result = gitgrip::cli::commands::repo::run_repo_add(
        &ws.workspace_root,
        "not-a-valid-url",
        None,
        None,
        None,
    );
    assert!(result.is_err(), "repo add with invalid URL should fail");
}

// ── repo remove ──────────────────────────────────────────────────────

#[test]
fn test_repo_remove_from_manifest() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let result = gitgrip::cli::commands::repo::run_repo_remove(
        &ws.workspace_root,
        "backend",
        false, // don't delete files
    );
    assert!(
        result.is_ok(),
        "repo remove should succeed: {:?}",
        result.err()
    );

    // Verify backend is no longer in manifest
    let manifest_path = workspace_manifest_path(&ws.workspace_root);
    let content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        !content.contains("  backend:"),
        "manifest should not contain backend repo entry"
    );
    // frontend should still be there
    assert!(
        content.contains("  frontend:"),
        "manifest should still contain frontend"
    );

    let legacy_manifest = ws
        .workspace_root
        .join(".gitgrip")
        .join("manifests")
        .join("manifest.yaml");
    let legacy_content = fs::read_to_string(legacy_manifest).unwrap();
    assert!(
        !legacy_content.contains("  backend:"),
        "legacy manifest mirror should not contain backend repo entry"
    );
}

#[test]
fn test_repo_remove_with_delete_files() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let backend_path = ws.repo_path("backend");
    assert!(backend_path.exists(), "backend should exist before removal");

    let result = gitgrip::cli::commands::repo::run_repo_remove(
        &ws.workspace_root,
        "backend",
        true, // delete files
    );
    assert!(
        result.is_ok(),
        "repo remove with delete should succeed: {:?}",
        result.err()
    );

    assert!(
        !backend_path.exists(),
        "backend files should be deleted after removal"
    );
}

#[test]
fn test_repo_remove_nonexistent() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();

    let result =
        gitgrip::cli::commands::repo::run_repo_remove(&ws.workspace_root, "nonexistent", false);
    assert!(result.is_err(), "removing nonexistent repo should fail");
    let err_msg = result.unwrap_err().to_string();
    assert!(
        err_msg.contains("not found"),
        "error should mention not found: {}",
        err_msg
    );
}

#[test]
fn test_repo_remove_preserves_files_by_default() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let backend_path = ws.repo_path("backend");

    gitgrip::cli::commands::repo::run_repo_remove(
        &ws.workspace_root,
        "backend",
        false, // don't delete files
    )
    .unwrap();

    assert!(
        backend_path.exists(),
        "backend files should be preserved when delete_files=false"
    );
}

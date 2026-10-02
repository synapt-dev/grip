//! Integration tests for the group command and group filtering.

mod common;

use common::assertions::assert_on_branch;
use common::fixtures::WorkspaceBuilder;
use std::fs;

#[test]
fn test_group_list_shows_groups() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("frontend", vec!["core", "ui"])
        .add_repo_with_groups("backend", vec!["core", "api"])
        .add_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::group::run_group_list(&ws.workspace_root, &manifest);
    assert!(
        result.is_ok(),
        "group list should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_with_group_filter() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("frontend", vec!["ui"])
        .add_repo_with_groups("backend", vec!["api"])
        .add_repo_with_groups("shared", vec!["ui", "api"])
        .build();

    let manifest = ws.load_manifest();

    // Create branch only in "ui" group repos
    let group = vec!["ui".to_string()];
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/ui-fix"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: Some(&group),
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch with group filter should succeed: {:?}",
        result.err()
    );

    // frontend and shared are in "ui" group
    assert_on_branch(&ws.repo_path("frontend"), "feat/ui-fix");
    assert_on_branch(&ws.repo_path("shared"), "feat/ui-fix");
    // backend is NOT in "ui" group
    assert_on_branch(&ws.repo_path("backend"), "main");
}

#[test]
fn test_group_filter_empty_group() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("frontend", vec!["ui"])
        .add_repo_with_groups("backend", vec!["api"])
        .build();

    let manifest = ws.load_manifest();

    // Filter by a group that doesn't exist
    let group = vec!["nonexistent".to_string()];
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/empty"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: Some(&group),
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch with empty group should succeed gracefully: {:?}",
        result.err()
    );

    // Neither repo should have the branch (both still on main)
    assert_on_branch(&ws.repo_path("frontend"), "main");
    assert_on_branch(&ws.repo_path("backend"), "main");
}

#[test]
fn test_repos_without_groups_default_empty() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();
    let config = manifest.repos.get("app").unwrap();
    assert!(config.groups.is_empty());
}

#[test]
fn test_manifest_groups_parse() {
    let yaml = r#"
version: 1
repos:
  frontend:
    url: git@github.com:user/frontend.git
    path: frontend
    groups: [core, ui]
  backend:
    url: git@github.com:user/backend.git
    path: backend
    groups: [core, api]
  docs:
    url: git@github.com:user/docs.git
    path: docs
"#;
    let manifest = gitgrip::core::manifest::Manifest::parse(yaml).unwrap();

    let frontend = manifest.repos.get("frontend").unwrap();
    assert_eq!(frontend.groups, vec!["core", "ui"]);

    let backend = manifest.repos.get("backend").unwrap();
    assert_eq!(backend.groups, vec!["core", "api"]);

    let docs = manifest.repos.get("docs").unwrap();
    assert!(docs.groups.is_empty());
}

#[test]
fn test_group_add_updates_legacy_manifest_mirror() {
    let ws = WorkspaceBuilder::new().add_repo("frontend").build();
    let repos = vec!["frontend".to_string()];

    let result = gitgrip::cli::commands::group::run_group_add(&ws.workspace_root, "core", &repos);
    assert!(
        result.is_ok(),
        "group add should succeed: {:?}",
        result.err()
    );

    let legacy_manifest = ws
        .workspace_root
        .join(".gitgrip")
        .join("manifests")
        .join("manifest.yaml");
    let legacy_content = fs::read_to_string(legacy_manifest).unwrap();
    assert!(
        legacy_content.contains("groups"),
        "legacy manifest mirror should be updated with group changes"
    );
    assert!(
        legacy_content.contains("core"),
        "legacy manifest mirror should contain added group"
    );
}

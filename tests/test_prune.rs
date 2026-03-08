//! Tests for the prune command

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_prune_dry_run_lists_merged_branches() {
    let ws = WorkspaceBuilder::new().add_repo("alpha").build();

    let repo_path = ws.repo_path("alpha");

    // Create a feature branch, make a commit, merge it back
    git_helpers::create_branch(&repo_path, "feat/merged");
    git_helpers::commit_file(&repo_path, "feature.txt", "content", "Add feature");
    git_helpers::checkout(&repo_path, "main");

    // Merge the feature branch
    std::process::Command::new("git")
        .args(["merge", "feat/merged", "--no-ff", "-m", "Merge feat/merged"])
        .current_dir(&repo_path)
        .output()
        .unwrap();

    // Verify the branch is merged
    let repo = gitgrip::git::open_repo(&repo_path).unwrap();
    let is_merged = gitgrip::git::branch::is_branch_merged(&repo, "feat/merged", "main").unwrap();
    assert!(is_merged, "Branch should be merged");

    // Run prune (dry-run by default)
    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::prune::run_prune(
        &ws.workspace_root,
        &manifest,
        false, // dry-run
        false,
        None,
        None,
    );
    assert!(result.is_ok());

    // Branch should still exist (dry-run)
    assert!(git_helpers::branch_exists(&repo_path, "feat/merged"));
}

#[test]
fn test_prune_execute_deletes_merged_branches() {
    let ws = WorkspaceBuilder::new().add_repo("alpha").build();

    let repo_path = ws.repo_path("alpha");

    // Create a feature branch, make a commit, merge it
    git_helpers::create_branch(&repo_path, "feat/to-delete");
    git_helpers::commit_file(&repo_path, "feature.txt", "content", "Add feature");
    git_helpers::checkout(&repo_path, "main");

    std::process::Command::new("git")
        .args([
            "merge",
            "feat/to-delete",
            "--no-ff",
            "-m",
            "Merge feat/to-delete",
        ])
        .current_dir(&repo_path)
        .output()
        .unwrap();

    // Run prune with --execute
    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::prune::run_prune(
        &ws.workspace_root,
        &manifest,
        true, // execute
        false,
        None,
        None,
    );
    assert!(result.is_ok());

    // Branch should be gone
    assert!(!git_helpers::branch_exists(&repo_path, "feat/to-delete"));
}

#[test]
fn test_prune_skips_current_and_default() {
    let ws = WorkspaceBuilder::new().add_repo("alpha").build();

    let repo_path = ws.repo_path("alpha");

    // Run prune — should not try to delete the default branch
    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::prune::run_prune(
        &ws.workspace_root,
        &manifest,
        true, // execute
        false,
        None,
        None,
    );
    assert!(result.is_ok());

    // Default branch still exists
    assert!(git_helpers::branch_exists(&repo_path, "main"));
}

#[test]
fn test_prune_no_merged_branches() {
    let ws = WorkspaceBuilder::new().add_repo("alpha").build();

    let repo_path = ws.repo_path("alpha");

    // Create a feature branch with unmerged commits
    git_helpers::create_branch(&repo_path, "feat/unmerged");
    git_helpers::commit_file(&repo_path, "feature.txt", "content", "Add feature");
    git_helpers::checkout(&repo_path, "main");

    // Run prune — should report nothing to prune
    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::prune::run_prune(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        None,
        None,
    );
    assert!(result.is_ok());

    // Unmerged branch should still exist
    assert!(git_helpers::branch_exists(&repo_path, "feat/unmerged"));
}

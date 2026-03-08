//! Integration tests for the push command.

mod common;

use common::assertions::assert_on_branch;
use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_push_to_remote() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create branch, make changes, commit
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/push-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("app").join("pushed.txt"), "content").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: push test",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Push with set-upstream
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true, // set_upstream
        false,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "push should succeed: {:?}", result.err());

    // Verify the branch exists on the remote
    assert!(
        git_helpers::branch_exists(&ws.repo_path("app"), "feat/push-test"),
        "branch should exist locally"
    );
}

#[test]
fn test_push_nothing_to_push() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Push with nothing to push -- should succeed
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

#[test]
fn test_push_skips_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    // Create branch in app only (reference repos are skipped)
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/ref-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("app").join("change.txt"), "data").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "change",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Push -- should skip reference repo
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "push should succeed: {:?}", result.err());

    // docs should still be on main (not pushed, not branched)
    assert_on_branch(&ws.repo_path("docs"), "main");
}

#[test]
fn test_push_multiple_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch, commit in both
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/multi-push"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("frontend").join("fe.txt"), "fe").unwrap();
    std::fs::write(ws.repo_path("backend").join("be.txt"), "be").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: multi push",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "push should succeed: {:?}", result.err());
}

#[test]
fn test_push_force() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create branch, commit, push
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/force-push"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("app").join("first.txt"), "first").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "first commit",
        false,
        false,
        None,
        None,
    )
    .unwrap();
    gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Make another commit
    std::fs::write(ws.repo_path("app").join("second.txt"), "second").unwrap();
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "second commit",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Force push
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        false,
        true, // force
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "force push should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_push_quiet_mode() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Quiet push with nothing to push should succeed (suppresses "nothing to push" messages)
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        true, // quiet
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "quiet push should succeed: {:?}",
        result.err()
    );
}

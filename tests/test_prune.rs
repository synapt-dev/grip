//! Tests for the prune command

mod common;

use assert_cmd::Command as AssertCommand;
use predicates::prelude::*;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

/// Put a fixture into the shape production actually runs in: manifest target is
/// `dev`, `dev` is checked out, and `main` exists as the release branch. Before
/// 2026-08-01 target and default were both `main`, which protected `main` by
/// coincidence; every prune test still encodes that retired arrangement.
fn dev_target_workspace(repo: &str) -> common::fixtures::WorkspaceFixture {
    let ws = WorkspaceBuilder::new().add_repo(repo).build();
    let manifest_path = ws
        .workspace_root
        .join(".gitgrip")
        .join("spaces")
        .join("main")
        .join("gripspace.yml");
    let yaml = std::fs::read_to_string(&manifest_path).unwrap();
    assert!(
        yaml.contains("default_branch: main"),
        "fixture no longer declares a target this helper knows how to move: {yaml}"
    );
    std::fs::write(
        &manifest_path,
        yaml.replace("default_branch: main", "default_branch: dev"),
    )
    .unwrap();

    let repo_path = ws.repo_path(repo);
    git_helpers::create_branch(&repo_path, "dev");
    assert!(git_helpers::branch_exists(&repo_path, "main"));
    ws
}

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

#[test]
fn test_prune_protects_main_when_target_is_dev() {
    // The production shape: target `dev`, standing on `dev`, `main` present.
    // `main` is neither current nor target here, which is exactly the state the
    // old two-slot guard left unprotected.
    let ws = dev_target_workspace("alpha");
    let repo_path = ws.repo_path("alpha");

    // A genuinely merged branch, so this test also proves prune still WORKS.
    // Without it, a fix that protected everything would pass just as happily.
    git_helpers::create_branch(&repo_path, "feat/spent");
    git_helpers::commit_file(&repo_path, "spent.txt", "x", "spent work");
    git_helpers::checkout(&repo_path, "dev");
    std::process::Command::new("git")
        .args(["merge", "feat/spent", "--no-ff", "-m", "merge spent"])
        .current_dir(&repo_path)
        .output()
        .unwrap();

    AssertCommand::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .args(["prune", "--execute", "--repo", "alpha"])
        .assert()
        .success();

    assert!(
        git_helpers::branch_exists(&repo_path, "main"),
        "the release branch must survive a prune run from dev"
    );
    assert!(git_helpers::branch_exists(&repo_path, "dev"));
    assert!(
        !git_helpers::branch_exists(&repo_path, "feat/spent"),
        "positive control: prune must still delete a merged branch, or this test \
         would pass against a guard that simply protects everything"
    );
}

#[test]
fn test_prune_protects_more_and_says_so_when_default_is_unresolvable() {
    // The default is made GENUINELY unresolvable -- origin/HEAD is deleted from a
    // real clone -- rather than stubbed. A stub would assert the code path against
    // a fixture instead of against the condition, and the real failure (a clone
    // that was never given an origin/HEAD) would go unexercised.
    let ws = dev_target_workspace("alpha");
    let repo_path = ws.repo_path("alpha");

    let before = std::process::Command::new("git")
        .args(["symbolic-ref", "refs/remotes/origin/HEAD"])
        .current_dir(&repo_path)
        .output()
        .unwrap();
    assert!(
        before.status.success(),
        "control: the clone must HAVE an origin/HEAD before we remove it, or this \
         test proves nothing about removing it"
    );

    std::process::Command::new("git")
        .args(["symbolic-ref", "--delete", "refs/remotes/origin/HEAD"])
        .current_dir(&repo_path)
        .output()
        .unwrap();
    let after = std::process::Command::new("git")
        .args(["symbolic-ref", "refs/remotes/origin/HEAD"])
        .current_dir(&repo_path)
        .output()
        .unwrap();
    assert!(
        !after.status.success(),
        "the removal must actually make resolution fail"
    );

    AssertCommand::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .args(["prune", "--execute", "--repo", "alpha"])
        .assert()
        .success()
        // Protecting more must not be silent. A silent protected-more reads as
        // "the default resolved fine" to the next person who runs this.
        // NOTE: Output::warning writes to stdout, not stderr -- asserted against
        // the stream the binary actually uses, verified by running it.
        .stdout(predicate::str::contains(
            "could not determine the default branch",
        ));

    assert!(git_helpers::branch_exists(&repo_path, "main"));
    assert!(git_helpers::branch_exists(&repo_path, "dev"));
}

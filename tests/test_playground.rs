//! Proving-ground tests for the team-workspace playground harness.
//!
//! These tests intentionally drive the `gr` binary against disposable local
//! repos. The goal is to validate the workspace lifecycle in a safe, offline
//! environment before later slices add cache-backed materialization and agent
//! workspace flows.

mod common;

use common::assertions::{assert_branch_not_exists, assert_file_exists, assert_on_branch};
use common::git_helpers;
use common::playground::PlaygroundHarness;

#[test]
fn test_playground_cli_flow_init_sync_branch_checkout_and_prune() {
    let playground = PlaygroundHarness::new(&["frontend", "backend"]);

    playground.init_from_dirs();

    let manifest_path = playground
        .workspace_root
        .join(".gitgrip")
        .join("spaces")
        .join("main")
        .join("gripspace.yml");
    assert_file_exists(&manifest_path);

    std::fs::remove_dir_all(playground.repo_path("backend")).expect("failed to remove backend");
    assert!(
        !playground.repo_path("backend").exists(),
        "backend repo should be removed before sync"
    );

    playground.run_in_workspace(["sync"]);
    assert_file_exists(&playground.repo_path("backend").join(".git"));

    playground.run_in_workspace(["branch", "feat/playground"]);
    for repo_name in &playground.repo_names {
        assert_on_branch(&playground.repo_path(repo_name), "feat/playground");
    }

    git_helpers::commit_file(
        &playground.repo_path("frontend"),
        "playground.txt",
        "frontend playground content\n",
        "Add frontend playground change",
    );
    git_helpers::commit_file(
        &playground.repo_path("backend"),
        "playground.txt",
        "backend playground content\n",
        "Add backend playground change",
    );

    playground.run_in_workspace(["checkout", "main"]);
    for repo_name in &playground.repo_names {
        assert_on_branch(&playground.repo_path(repo_name), "main");
    }

    for repo_name in &playground.repo_names {
        let repo_path = playground.repo_path(repo_name);
        let output = std::process::Command::new("git")
            .args([
                "merge",
                "feat/playground",
                "--no-ff",
                "-m",
                "Merge feat/playground",
            ])
            .current_dir(&repo_path)
            .output()
            .expect("failed to run git merge");
        assert!(
            output.status.success(),
            "git merge failed in {}: {}",
            repo_path.display(),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    playground.run_in_workspace(["prune", "--execute"]);
    for repo_name in &playground.repo_names {
        assert_branch_not_exists(&playground.repo_path(repo_name), "feat/playground");
        assert_on_branch(&playground.repo_path(repo_name), "main");
    }
}

#[test]
fn test_playground_sync_cli_pulls_upstream_change() {
    let playground = PlaygroundHarness::new(&["frontend"]);

    playground.init_from_dirs();

    let staging = playground._temp.path().join("sync-staging-frontend");
    git_helpers::clone_repo(&playground.remote_url("frontend"), &staging);
    git_helpers::commit_file(
        &staging,
        "upstream.txt",
        "new upstream content\n",
        "Add upstream change",
    );
    git_helpers::push_branch(&staging, "origin", "main");

    let status_output = playground.run_in_workspace_output(["status", "--quiet"]);
    assert!(
        status_output.status.success(),
        "status should succeed before sync: {}",
        String::from_utf8_lossy(&status_output.stderr)
    );
    let stdout = String::from_utf8_lossy(&status_output.stdout);
    assert!(
        stdout.contains("SUMMARY:"),
        "quiet status should include summary line, got:\n{}",
        stdout
    );

    playground.run_in_workspace(["sync"]);

    assert_file_exists(&playground.repo_path("frontend").join("upstream.txt"));
    assert_on_branch(&playground.repo_path("frontend"), "main");
}

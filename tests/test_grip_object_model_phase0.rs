//! TDD stubs for the grip object model Phase 0.
//!
//! These tests intentionally target the user-facing `gr` binary and define the
//! minimum behavior Apollo's first snapshot/log/diff/checkout slice must
//! satisfy. They are expected to fail until the new grip snapshot commands
//! exist on the sprint branch.

mod common;

use common::git_helpers;
use common::playground::PlaygroundHarness;

fn stdout(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

#[test]
fn test_grip_snapshot_bootstraps_dedicated_repo_and_commits_repo_state() {
    let playground = PlaygroundHarness::new(&["recall", "premium", "grip", "config", "site"]);
    playground.init_from_dirs();

    let output = playground.run_in_workspace_output(["grip", "--type", "ceremony"]);
    assert!(
        output.status.success(),
        "gr grip should succeed once implemented:\nstdout:\n{}\nstderr:\n{}",
        stdout(&output),
        stderr(&output)
    );

    assert!(
        playground.workspace_root.join(".grip").join(".git").exists(),
        "gr grip should bootstrap a dedicated .grip git repo"
    );
}

#[test]
fn test_grip_show_is_human_reviewable() {
    let playground = PlaygroundHarness::new(&["recall", "premium"]);
    playground.init_from_dirs();

    let snap = playground.run_in_workspace_output(["grip", "--type", "snapshot"]);
    assert!(
        snap.status.success(),
        "gr grip should create a snapshot before show:\nstdout:\n{}\nstderr:\n{}",
        stdout(&snap),
        stderr(&snap)
    );

    let output = playground.run_in_workspace_output(["show", "HEAD"]);
    assert!(
        output.status.success(),
        "gr show should succeed for the latest grip snapshot:\nstdout:\n{}\nstderr:\n{}",
        stdout(&output),
        stderr(&output)
    );

    let rendered = stdout(&output);
    assert!(
        rendered.contains("recall") && rendered.contains("premium"),
        "gr show should render repo names in a reviewable form, got:\n{}",
        rendered
    );
}

#[test]
fn test_grip_diff_reports_changed_repos_between_snapshots() {
    let playground = PlaygroundHarness::new(&["recall", "premium"]);
    playground.init_from_dirs();

    let first = playground.run_in_workspace_output(["grip", "--type", "snapshot"]);
    assert!(
        first.status.success(),
        "first snapshot should succeed:\nstdout:\n{}\nstderr:\n{}",
        stdout(&first),
        stderr(&first)
    );

    git_helpers::commit_file(
        &playground.repo_path("recall"),
        "phase0.txt",
        "snapshot drift\n",
        "Add phase0 drift",
    );

    let second = playground.run_in_workspace_output(["grip", "--type", "snapshot"]);
    assert!(
        second.status.success(),
        "second snapshot should succeed:\nstdout:\n{}\nstderr:\n{}",
        stdout(&second),
        stderr(&second)
    );

    let output = playground.run_in_workspace_output(["diff", "HEAD~1", "HEAD"]);
    assert!(
        output.status.success(),
        "gr diff should succeed across grip snapshots:\nstdout:\n{}\nstderr:\n{}",
        stdout(&output),
        stderr(&output)
    );

    let rendered = stdout(&output);
    assert!(
        rendered.contains("recall"),
        "gr diff should identify the changed repo, got:\n{}",
        rendered
    );
}

#[test]
fn test_grip_checkout_restores_prior_snapshot_repo_heads() {
    let playground = PlaygroundHarness::new(&["recall"]);
    playground.init_from_dirs();

    let before_sha = git_helpers::get_head_sha(&playground.repo_path("recall"));

    let first = playground.run_in_workspace_output(["grip", "--type", "snapshot"]);
    assert!(
        first.status.success(),
        "initial snapshot should succeed:\nstdout:\n{}\nstderr:\n{}",
        stdout(&first),
        stderr(&first)
    );

    git_helpers::commit_file(
        &playground.repo_path("recall"),
        "restore.txt",
        "later change\n",
        "Add later change",
    );
    let after_sha = git_helpers::get_head_sha(&playground.repo_path("recall"));
    assert_ne!(before_sha, after_sha, "test setup requires a changed repo head");

    let output = playground.run_in_workspace_output(["checkout", "HEAD~1"]);
    assert!(
        output.status.success(),
        "gr checkout should restore the earlier grip snapshot:\nstdout:\n{}\nstderr:\n{}",
        stdout(&output),
        stderr(&output)
    );

    assert_eq!(
        git_helpers::get_head_sha(&playground.repo_path("recall")),
        before_sha,
        "gr checkout should restore repo head to the recorded snapshot"
    );
}

#[test]
fn test_grip_snapshot_handles_detached_head_explicitly() {
    let playground = PlaygroundHarness::new(&["recall"]);
    playground.init_from_dirs();

    let repo_path = playground.repo_path("recall");
    let head = git_helpers::get_head_sha(&repo_path);
    let detach = std::process::Command::new("git")
        .args(["checkout", "--detach", &head])
        .current_dir(&repo_path)
        .output()
        .expect("failed to detach HEAD");
    assert!(
        detach.status.success(),
        "failed to set up detached HEAD: {}",
        String::from_utf8_lossy(&detach.stderr)
    );

    let output = playground.run_in_workspace_output(["grip", "--type", "snapshot"]);
    assert!(
        output.status.success(),
        "phase 0 must define detached-HEAD behavior explicitly; current failure:\nstdout:\n{}\nstderr:\n{}",
        stdout(&output),
        stderr(&output)
    );
}

#[test]
fn test_grip_show_fails_cleanly_when_grip_repo_is_missing() {
    let playground = PlaygroundHarness::new(&["recall"]);
    playground.init_from_dirs();

    let output = playground.run_in_workspace_output(["show", "HEAD"]);
    assert!(
        !output.status.success(),
        "gr show should fail before any .grip repo exists"
    );
}

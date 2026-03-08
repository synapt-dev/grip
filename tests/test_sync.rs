//! Integration tests for the sync command.

mod common;

use common::assertions::{assert_file_exists, assert_on_branch};
use common::fixtures::{write_griptree_config, WorkspaceBuilder};
use common::git_helpers;
use std::fs;
use std::path::Path;
use std::process::Command;

fn git(dir: &Path, args: &[&str]) {
    let output = Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .unwrap_or_else(|e| panic!("failed to run git {:?}: {}", args, e));
    assert!(
        output.status.success(),
        "git {:?} failed in {}: {}",
        args,
        dir.display(),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[tokio::test]
async fn test_sync_clones_missing_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    // Remove one repo to simulate "not cloned"
    std::fs::remove_dir_all(ws.repo_path("backend")).unwrap();
    assert!(!ws.repo_path("backend").exists());

    let manifest = ws.load_manifest();

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
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    // backend should now be cloned
    assert!(ws.repo_path("backend").join(".git").exists());
    assert_on_branch(&ws.repo_path("backend"), "main");
}

#[tokio::test]
async fn test_sync_pulls_existing_repos() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Push a new commit to the bare remote (simulating upstream changes)
    let staging = ws._temp.path().join("sync-staging");
    git_helpers::clone_repo(&ws.remote_url("app"), &staging);
    git_helpers::commit_file(&staging, "new-file.txt", "content", "Add new file");
    git_helpers::push_branch(&staging, "origin", "main");

    let manifest = ws.load_manifest();

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
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    // The new file should now exist in the workspace repo
    assert_file_exists(&ws.repo_path("app").join("new-file.txt"));
}

#[tokio::test]
async fn test_sync_uses_griptree_upstream_mapping() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let staging = ws._temp.path().join("sync-upstream-staging");
    git_helpers::clone_repo(&ws.remote_url("app"), &staging);
    git_helpers::create_branch(&staging, "dev");
    git_helpers::commit_file(&staging, "dev-only.txt", "dev", "Add dev file");
    git_helpers::push_branch(&staging, "origin", "dev");

    git_helpers::create_branch(&ws.repo_path("app"), "feat/griptree");

    write_griptree_config(&ws.workspace_root, "feat/griptree", "app", "origin/dev");
    let manifest = ws.load_manifest();

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
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    assert_file_exists(&ws.repo_path("app").join("dev-only.txt"));
}

#[tokio::test]
async fn test_sync_sets_tracking_upstream_for_griptree_base_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    git_helpers::create_branch(&ws.repo_path("app"), "feat/griptree");
    assert_eq!(
        git_helpers::branch_upstream(&ws.repo_path("app"), "feat/griptree"),
        None
    );

    write_griptree_config(&ws.workspace_root, "feat/griptree", "app", "origin/main");
    let manifest = ws.load_manifest();

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
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    assert_eq!(
        git_helpers::branch_upstream(&ws.repo_path("app"), "feat/griptree"),
        Some("origin/main".to_string())
    );
}

#[tokio::test]
async fn test_sync_handles_up_to_date() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Sync when already up to date
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
    assert!(
        result.is_ok(),
        "sync should succeed when up to date: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_sync_skips_griptree_base_with_local_commits_ahead() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    git_helpers::create_branch(&ws.repo_path("app"), "feat/griptree");
    git_helpers::commit_file(
        &ws.repo_path("app"),
        "local-only.txt",
        "local",
        "Add local-only file",
    );

    let staging = ws._temp.path().join("sync-diverge-staging");
    git_helpers::clone_repo(&ws.remote_url("app"), &staging);
    git_helpers::commit_file(&staging, "upstream.txt", "upstream", "Add upstream file");
    git_helpers::push_branch(&staging, "origin", "main");

    write_griptree_config(&ws.workspace_root, "feat/griptree", "app", "origin/main");
    let manifest = ws.load_manifest();

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
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    assert_file_exists(&ws.repo_path("app").join("local-only.txt"));
    assert!(
        !ws.repo_path("app").join("upstream.txt").exists(),
        "expected sync to skip pulling upstream changes"
    );
    assert_on_branch(&ws.repo_path("app"), "feat/griptree");
}

#[tokio::test]
async fn test_sync_reset_refs_hard_resets_reference_repo() {
    let ws = WorkspaceBuilder::new().add_reference_repo("ref").build();

    let remote_sha = git_helpers::get_head_sha(&ws.remote_path("ref"));

    git_helpers::commit_file(
        &ws.repo_path("ref"),
        "local-only.txt",
        "local",
        "Add local-only file",
    );

    let local_sha = git_helpers::get_head_sha(&ws.repo_path("ref"));
    assert_ne!(local_sha, remote_sha);

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
        true,
        false,
        false,
    )
    .await;
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    let synced_sha = git_helpers::get_head_sha(&ws.repo_path("ref"));
    let remote_sha_after = git_helpers::get_head_sha(&ws.remote_path("ref"));
    assert_eq!(synced_sha, remote_sha_after);
    assert!(
        !ws.repo_path("ref").join("local-only.txt").exists(),
        "expected reset to discard local changes"
    );
}

#[tokio::test]
async fn test_sync_reset_refs_checks_out_upstream_branch() {
    let ws = WorkspaceBuilder::new().add_reference_repo("ref").build();

    let staging = ws._temp.path().join("sync-ref-staging");
    git_helpers::clone_repo(&ws.remote_url("ref"), &staging);
    git_helpers::create_branch(&staging, "dev");
    git_helpers::commit_file(&staging, "dev-only.txt", "dev", "Add dev file");
    git_helpers::push_branch(&staging, "origin", "dev");

    git_helpers::create_branch(&ws.repo_path("ref"), "codi-gripspace");

    write_griptree_config(&ws.workspace_root, "feat/griptree", "ref", "origin/dev");
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
        true,
        false,
        false,
    )
    .await;
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    assert_on_branch(&ws.repo_path("ref"), "dev");
    assert_file_exists(&ws.repo_path("ref").join("dev-only.txt"));
}

#[tokio::test]
async fn test_sync_reset_refs_falls_back_to_detached_when_branch_locked_in_worktree() {
    let ws = WorkspaceBuilder::new().add_reference_repo("ref").build();

    let staging = ws._temp.path().join("sync-ref-locked-branch-staging");
    git_helpers::clone_repo(&ws.remote_url("ref"), &staging);
    git_helpers::create_branch(&staging, "dev");
    git_helpers::commit_file(&staging, "dev-only.txt", "dev", "Add dev file");
    git_helpers::push_branch(&staging, "origin", "dev");

    let ref_repo = ws.repo_path("ref");
    git(&ref_repo, &["fetch", "origin", "dev:dev"]);

    let locked_worktree = ws._temp.path().join("ref-dev-worktree");
    git(
        &ref_repo,
        &["worktree", "add", locked_worktree.to_str().unwrap(), "dev"],
    );

    git_helpers::commit_file(&ref_repo, "local-only.txt", "local", "Add local-only file");

    write_griptree_config(&ws.workspace_root, "feat/griptree", "ref", "origin/dev");
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        false,
        true,
        false,
        false,
    )
    .await;
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    assert_file_exists(&ref_repo.join("dev-only.txt"));
    assert!(
        !ref_repo.join("local-only.txt").exists(),
        "expected reset to discard local changes"
    );

    let repo = gitgrip::git::open_repo(&ref_repo).expect("open repo");
    let head = gitgrip::git::get_current_branch(&repo).expect("current branch");
    assert!(
        head.starts_with("(HEAD detached at "),
        "expected detached HEAD fallback, got: {}",
        head
    );

    git(
        &ref_repo,
        &[
            "worktree",
            "remove",
            "--force",
            locked_worktree.to_str().unwrap(),
        ],
    );
}

#[tokio::test]
async fn test_sync_multiple_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .add_repo("gamma")
        .build();

    // Remove alpha and beta to test clone
    std::fs::remove_dir_all(ws.repo_path("alpha")).unwrap();
    std::fs::remove_dir_all(ws.repo_path("beta")).unwrap();

    let manifest = ws.load_manifest();

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
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());

    // All should now be cloned
    assert!(ws.repo_path("alpha").join(".git").exists());
    assert!(ws.repo_path("beta").join(".git").exists());
    assert!(ws.repo_path("gamma").join(".git").exists());
}

#[tokio::test]
async fn test_sync_quiet_mode() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Quiet sync on already-synced repos should succeed (suppresses "up to date" messages)
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        true,
        None,
        None,
        false,
        false,
        false,
        false,
    )
    .await;
    assert!(
        result.is_ok(),
        "quiet sync should succeed: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_sync_sequential_mode() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Sequential sync (--sequential flag)
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        None,
        None,
        true,
        false,
        false,
        false,
    )
    .await;
    assert!(
        result.is_ok(),
        "sequential sync should succeed: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_sync_clone_failure_invalid_url() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let mut manifest = ws.load_manifest();

    // Force clone path: delete repo and replace URL with invalid path
    fs::remove_dir_all(ws.repo_path("app")).unwrap();
    assert!(!ws.repo_path("app").exists());
    manifest.repos.get_mut("app").expect("app repo config").url =
        Some("file:///does-not-exist/repo.git".to_string());

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
    assert!(result.is_ok(), "sync should not crash: {:?}", result.err());

    // Clone should fail, leaving no git metadata
    assert!(
        !ws.repo_path("app").join(".git").exists(),
        "expected clone to fail without .git"
    );
}

#[tokio::test]
async fn test_sync_existing_repo_missing_git_dir() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Corrupt repo by removing .git
    fs::remove_dir_all(ws.repo_path("app").join(".git")).unwrap();
    assert!(!ws.repo_path("app").join(".git").exists());

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
    assert!(result.is_ok(), "sync should not crash: {:?}", result.err());

    // Sync should report error and leave repo unchanged (still missing .git)
    assert!(
        !ws.repo_path("app").join(".git").exists(),
        "expected sync to fail for non-git directory"
    );
}

/// Helper to append workspace hooks to the manifest YAML
fn append_hooks_to_manifest(workspace_root: &Path, hooks_yaml: &str) {
    let manifest_path = workspace_root
        .join(".gitgrip")
        .join("spaces")
        .join("main")
        .join("gripspace.yml");
    let mut content = fs::read_to_string(&manifest_path).unwrap();
    content.push_str(hooks_yaml);
    fs::write(&manifest_path, content).unwrap();
}

#[tokio::test]
async fn test_sync_runs_post_sync_hooks_always() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Add a hook that creates a marker file (condition: always)
    let marker = ws.workspace_root.join("hook-ran.txt");
    let hooks_yaml = format!(
        r#"
workspace:
  hooks:
    post-sync:
      - name: create-marker
        command: echo "hook executed" > "{}"
        condition: always
"#,
        marker.display()
    );
    append_hooks_to_manifest(&ws.workspace_root, &hooks_yaml);

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        true,
        None,
        None,
        false,
        false,
        false,
        false,
    )
    .await;
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());
    assert!(marker.exists(), "hook marker file should have been created");
}

#[tokio::test]
async fn test_sync_hook_failure_is_warning_not_error() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Add a hook that fails
    let hooks_yaml = r#"
workspace:
  hooks:
    post-sync:
      - name: failing-hook
        command: exit 1
        condition: always
"#;
    append_hooks_to_manifest(&ws.workspace_root, hooks_yaml);

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        true,
        None,
        None,
        false,
        false,
        false,
        false,
    )
    .await;
    // Sync should still succeed even though hook failed
    assert!(
        result.is_ok(),
        "sync should succeed even when hook fails: {:?}",
        result.err()
    );
}

#[tokio::test]
async fn test_sync_no_hooks_flag_skips_hooks() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Add a hook that creates a marker file
    let marker = ws.workspace_root.join("no-hooks-marker.txt");
    let hooks_yaml = format!(
        r#"
workspace:
  hooks:
    post-sync:
      - name: create-marker
        command: echo "should not run" > "{}"
        condition: always
"#,
        marker.display()
    );
    append_hooks_to_manifest(&ws.workspace_root, &hooks_yaml);

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::sync::run_sync(
        &ws.workspace_root,
        &manifest,
        false,
        true,
        None,
        None,
        false,
        false,
        false,
        true, // no_hooks = true
    )
    .await;
    assert!(result.is_ok(), "sync should succeed: {:?}", result.err());
    assert!(
        !marker.exists(),
        "hook marker file should NOT exist when --no-hooks is set"
    );
}

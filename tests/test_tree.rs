//! Operational integration tests for griptree commands.
//!
//! Tests `gr tree add/list/remove/lock/unlock` against real git worktrees
//! in temporary workspaces. All tests run offline.

mod common;

use std::fs;
use std::path::Path;

use assert_cmd::Command;
use predicates::str::contains;
use serde_yaml::Value;

use common::assertions;
use common::fixtures::{write_griptree_config, WorkspaceBuilder};
use common::git_helpers;

fn set_default_branch(manifest_path: &Path, repo: &str, branch: &str) {
    let content = fs::read_to_string(manifest_path).unwrap();
    let mut doc: Value = serde_yaml::from_str(&content).unwrap();
    assert!(
        !doc["repos"][repo].is_null(),
        "repo {} missing from manifest",
        repo
    );
    doc["repos"][repo]["default_branch"] = Value::String(branch.to_string());
    fs::write(manifest_path, serde_yaml::to_string(&doc).unwrap()).unwrap();
}

// ── Tree Add ──────────────────────────────────────────────────────

#[test]
fn test_tree_add_creates_worktrees() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::tree::run_tree_add(
        &ws.workspace_root,
        &manifest,
        "feat/new-feature",
    );

    assert!(
        result.is_ok(),
        "tree add should succeed: {:?}",
        result.err()
    );

    // Griptree directory should be created as sibling to workspace
    let tree_path = ws.workspace_root.parent().unwrap().join("feat-new-feature");
    assertions::assert_file_exists(&tree_path);

    // .griptree pointer file should exist
    let pointer_path = tree_path.join(".griptree");
    assertions::assert_file_exists(&pointer_path);

    // Verify pointer contents
    let pointer: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&pointer_path).unwrap()).unwrap();
    assert_eq!(pointer["branch"], "feat/new-feature");
    assert_eq!(
        pointer["mainWorkspace"],
        ws.workspace_root.to_string_lossy().as_ref()
    );
    assert!(!pointer["locked"].as_bool().unwrap());

    // griptrees.json registry should be updated
    let registry_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    assertions::assert_file_exists(&registry_path);
    let registry: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&registry_path).unwrap()).unwrap();
    assert!(
        registry["griptrees"]["feat/new-feature"].is_object(),
        "registry should contain the new griptree"
    );

    // Each repo should have a worktree directory in the griptree
    assertions::assert_file_exists(&tree_path.join("app"));
    assertions::assert_file_exists(&tree_path.join("lib"));

    // Worktrees should be on the new branch
    assertions::assert_on_branch(&tree_path.join("app"), "feat/new-feature");
    assertions::assert_on_branch(&tree_path.join("lib"), "feat/new-feature");
}

#[test]
fn test_tree_add_writes_repo_upstreams() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    git_helpers::create_branch(&ws.repo_path("lib"), "dev");
    git_helpers::commit_file(&ws.repo_path("lib"), "dev.txt", "dev", "Add dev");
    git_helpers::push_branch(&ws.repo_path("lib"), "origin", "dev");
    git_helpers::checkout(&ws.repo_path("lib"), "main");

    let manifest_path =
        gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(&ws.workspace_root)
            .expect("workspace manifest path should resolve");
    set_default_branch(&manifest_path, "lib", "dev");
    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/upstream");
    assert!(
        result.is_ok(),
        "tree add should succeed: {:?}",
        result.err()
    );

    let tree_path = ws.workspace_root.parent().unwrap().join("feat-upstream");
    let config_path = tree_path.join(".gitgrip").join("griptree.json");
    let config: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&config_path).unwrap()).unwrap();

    assert_eq!(config["repoUpstreams"]["app"], "origin/main");
    assert_eq!(config["repoUpstreams"]["lib"], "origin/dev");

    assert_eq!(
        git_helpers::branch_upstream(&tree_path.join("app"), "feat/upstream"),
        Some("origin/main".to_string())
    );
    assert_eq!(
        git_helpers::branch_upstream(&tree_path.join("lib"), "feat/upstream"),
        Some("origin/dev".to_string())
    );
}

#[test]
fn test_tree_add_with_manifest_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .with_manifest_repo()
        .build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::tree::run_tree_add(
        &ws.workspace_root,
        &manifest,
        "feat/with-manifest",
    );

    assert!(
        result.is_ok(),
        "tree add with manifest repo should succeed: {:?}",
        result.err()
    );

    let tree_path = ws
        .workspace_root
        .parent()
        .unwrap()
        .join("feat-with-manifest");

    // Manifest worktree should be created in griptree
    let tree_manifest_dir = tree_path.join(".gitgrip").join("spaces").join("main");
    assertions::assert_file_exists(&tree_manifest_dir);

    // A supported manifest filename should exist in the griptree manifest dir.
    assert!(
        gitgrip::core::manifest_paths::resolve_manifest_file_in_dir(&tree_manifest_dir).is_some()
    );

    // Pointer should reference the manifest worktree
    let pointer_path = tree_path.join(".griptree");
    let pointer: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&pointer_path).unwrap()).unwrap();
    assert!(
        pointer["manifestBranch"].is_string(),
        "pointer should have manifestBranch"
    );
}

#[test]
fn test_tree_add_duplicate_fails() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Create first griptree
    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/dup")
        .expect("first tree add should succeed");

    // Attempt to create same griptree again
    let result =
        gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/dup");

    assert!(result.is_err(), "duplicate tree add should fail");
    let err = result.unwrap_err().to_string();
    assert!(
        err.contains("already exists"),
        "error should mention 'already exists': {}",
        err
    );
}

// ── Tree List ──────────────────────────────────────────────────────

#[test]
fn test_tree_list_empty() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let result = gitgrip::cli::commands::tree::run_tree_list(&ws.workspace_root);

    assert!(
        result.is_ok(),
        "tree list with no griptrees should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_tree_list_after_add() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/listed")
        .expect("tree add should succeed");

    let result = gitgrip::cli::commands::tree::run_tree_list(&ws.workspace_root);

    assert!(
        result.is_ok(),
        "tree list should succeed: {:?}",
        result.err()
    );

    // Verify registry has the entry
    let registry_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    let registry: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&registry_path).unwrap()).unwrap();
    assert!(registry["griptrees"]["feat/listed"].is_object());
}

#[test]
fn test_tree_list_from_griptree_workspace_shows_registered_tree() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/listed")
        .expect("tree add should succeed");

    let tree_path = ws.workspace_root.parent().unwrap().join("feat-listed");

    let mut cmd = Command::cargo_bin("gr").expect("gr binary should build");
    cmd.current_dir(&tree_path)
        .args(["tree", "list"])
        .assert()
        .success()
        .stdout(contains("feat/listed"));
}

// ── Tree Remove ──────────────────────────────────────────────────

#[test]
fn test_tree_remove() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/removeme")
        .expect("tree add should succeed");

    let tree_path = ws.workspace_root.parent().unwrap().join("feat-removeme");
    assert!(
        tree_path.exists(),
        "griptree dir should exist before remove"
    );

    let result =
        gitgrip::cli::commands::tree::run_tree_remove(&ws.workspace_root, "feat/removeme", false);

    assert!(
        result.is_ok(),
        "tree remove should succeed: {:?}",
        result.err()
    );

    // Directory should be gone
    assert!(!tree_path.exists(), "griptree directory should be removed");

    // Registry should no longer contain the entry
    let registry_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    let registry: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&registry_path).unwrap()).unwrap();
    assert!(
        registry["griptrees"]["feat/removeme"].is_null(),
        "registry should not contain removed griptree"
    );
}

#[test]
fn test_tree_remove_from_griptree_workspace() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(
        &ws.workspace_root,
        &manifest,
        "feat/remove-from-tree",
    )
    .expect("tree add should succeed");

    let tree_path = ws
        .workspace_root
        .parent()
        .unwrap()
        .join("feat-remove-from-tree");
    let result =
        gitgrip::cli::commands::tree::run_tree_remove(&tree_path, "feat/remove-from-tree", false);

    assert!(
        result.is_ok(),
        "tree remove from griptree workspace should succeed: {:?}",
        result.err()
    );
    assert!(!tree_path.exists(), "griptree directory should be removed");
}

#[test]
fn test_tree_remove_nonexistent_fails() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Create the griptrees.json so remove has something to read
    let config_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    std::fs::write(&config_path, r#"{"griptrees":{}}"#).unwrap();

    let result =
        gitgrip::cli::commands::tree::run_tree_remove(&ws.workspace_root, "feat/ghost", false);

    assert!(result.is_err(), "removing nonexistent griptree should fail");
    let err = result.unwrap_err().to_string();
    assert!(
        err.contains("not found"),
        "error should mention 'not found': {}",
        err
    );
}

// ── Tree Lock / Unlock ──────────────────────────────────────────

#[test]
fn test_tree_lock_prevents_remove() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/locked")
        .expect("tree add should succeed");

    // Lock the griptree
    let lock_result = gitgrip::cli::commands::tree::run_tree_lock(
        &ws.workspace_root,
        "feat/locked",
        Some("important work"),
    );
    assert!(
        lock_result.is_ok(),
        "tree lock should succeed: {:?}",
        lock_result.err()
    );

    // Verify registry shows locked
    let registry_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    let registry: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&registry_path).unwrap()).unwrap();
    assert!(
        registry["griptrees"]["feat/locked"]["locked"]
            .as_bool()
            .unwrap(),
        "registry should show locked"
    );
    assert_eq!(
        registry["griptrees"]["feat/locked"]["lock_reason"],
        "important work"
    );

    // Verify .griptree pointer also shows locked
    let tree_path = ws.workspace_root.parent().unwrap().join("feat-locked");
    let pointer: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(tree_path.join(".griptree")).unwrap())
            .unwrap();
    assert!(
        pointer["locked"].as_bool().unwrap(),
        "pointer should show locked"
    );

    // Try to remove without force - should fail
    let remove_result =
        gitgrip::cli::commands::tree::run_tree_remove(&ws.workspace_root, "feat/locked", false);
    assert!(
        remove_result.is_err(),
        "removing locked griptree should fail"
    );
    let err = remove_result.unwrap_err().to_string();
    assert!(
        err.contains("locked"),
        "error should mention 'locked': {}",
        err
    );

    // Force remove should succeed
    let force_result =
        gitgrip::cli::commands::tree::run_tree_remove(&ws.workspace_root, "feat/locked", true);
    assert!(
        force_result.is_ok(),
        "force remove should succeed: {:?}",
        force_result.err()
    );
    assert!(
        !tree_path.exists(),
        "griptree dir should be removed after force"
    );
}

#[test]
fn test_tree_lock_from_griptree_workspace() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(
        &ws.workspace_root,
        &manifest,
        "feat/lock-from-tree",
    )
    .expect("tree add should succeed");

    let tree_path = ws
        .workspace_root
        .parent()
        .unwrap()
        .join("feat-lock-from-tree");
    let lock_result =
        gitgrip::cli::commands::tree::run_tree_lock(&tree_path, "feat/lock-from-tree", None);
    assert!(
        lock_result.is_ok(),
        "tree lock from griptree workspace should succeed: {:?}",
        lock_result.err()
    );

    let registry_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    let registry: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&registry_path).unwrap()).unwrap();
    assert!(
        registry["griptrees"]["feat/lock-from-tree"]["locked"]
            .as_bool()
            .unwrap(),
        "registry should show locked after lock from griptree workspace"
    );
}

#[test]
fn test_tree_unlock_allows_remove() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/unlock-me")
        .expect("tree add should succeed");

    // Lock it
    gitgrip::cli::commands::tree::run_tree_lock(&ws.workspace_root, "feat/unlock-me", None)
        .expect("lock should succeed");

    // Unlock it
    let unlock_result =
        gitgrip::cli::commands::tree::run_tree_unlock(&ws.workspace_root, "feat/unlock-me");
    assert!(
        unlock_result.is_ok(),
        "unlock should succeed: {:?}",
        unlock_result.err()
    );

    // Verify registry shows unlocked
    let registry_path = ws.workspace_root.join(".gitgrip").join("griptrees.json");
    let registry: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&registry_path).unwrap()).unwrap();
    assert!(
        !registry["griptrees"]["feat/unlock-me"]["locked"]
            .as_bool()
            .unwrap(),
        "registry should show unlocked"
    );

    // Remove should now succeed
    let remove_result =
        gitgrip::cli::commands::tree::run_tree_remove(&ws.workspace_root, "feat/unlock-me", false);
    assert!(
        remove_result.is_ok(),
        "remove after unlock should succeed: {:?}",
        remove_result.err()
    );
}

// ── Tree Edge Cases ──────────────────────────────────────────────

#[test]
fn test_tree_add_branch_name_normalization() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let result = gitgrip::cli::commands::tree::run_tree_add(
        &ws.workspace_root,
        &manifest,
        "fix/bug/critical",
    );

    assert!(
        result.is_ok(),
        "tree add with nested slashes should succeed: {:?}",
        result.err()
    );

    // Branch slashes should become dashes in directory name
    let tree_path = ws.workspace_root.parent().unwrap().join("fix-bug-critical");
    assertions::assert_file_exists(&tree_path);
    assertions::assert_on_branch(&tree_path.join("app"), "fix/bug/critical");
}

#[test]
fn test_tree_original_repos_unaffected() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();
    let manifest = ws.load_manifest();

    // Verify repos start on main
    assertions::assert_on_branch(&ws.repo_path("app"), "main");
    assertions::assert_on_branch(&ws.repo_path("lib"), "main");

    gitgrip::cli::commands::tree::run_tree_add(
        &ws.workspace_root,
        &manifest,
        "feat/no-side-effects",
    )
    .expect("tree add should succeed");

    // Original repos should still be on main
    assertions::assert_on_branch(&ws.repo_path("app"), "main");
    assertions::assert_on_branch(&ws.repo_path("lib"), "main");
}

// ── Tree Return ────────────────────────────────────────────────

#[tokio::test]
async fn test_tree_return_checks_out_base_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    git_helpers::create_branch(&ws.repo_path("app"), "griptree-base");
    git_helpers::create_branch(&ws.repo_path("app"), "feat/return");

    write_griptree_config(&ws.workspace_root, "griptree-base", "app", "origin/main");

    let result = gitgrip::cli::commands::tree::run_tree_return(
        &ws.workspace_root,
        &manifest,
        &gitgrip::cli::commands::tree::TreeReturnOptions {
            base_override: None,
            no_sync: true,
            autostash: false,
            prune_branch: None,
            prune_current: false,
            prune_remote: false,
            force: false,
        },
    )
    .await;

    assert!(
        result.is_ok(),
        "tree return should succeed: {:?}",
        result.err()
    );
    assertions::assert_on_branch(&ws.repo_path("app"), "griptree-base");
}

#[tokio::test]
async fn test_tree_return_prunes_current_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    git_helpers::create_branch(&ws.repo_path("app"), "griptree-base");
    git_helpers::create_branch(&ws.repo_path("app"), "feat/prune");

    write_griptree_config(&ws.workspace_root, "griptree-base", "app", "origin/main");

    let result = gitgrip::cli::commands::tree::run_tree_return(
        &ws.workspace_root,
        &manifest,
        &gitgrip::cli::commands::tree::TreeReturnOptions {
            base_override: None,
            no_sync: true,
            autostash: false,
            prune_branch: None,
            prune_current: true,
            prune_remote: false,
            force: true,
        },
    )
    .await;

    assert!(
        result.is_ok(),
        "tree return should succeed: {:?}",
        result.err()
    );
    assert!(!git_helpers::branch_exists(
        &ws.repo_path("app"),
        "feat/prune"
    ));
    assertions::assert_on_branch(&ws.repo_path("app"), "griptree-base");
}

// ── Tree Add: independent clones (grip#861 spec) ───────────────────
//
// The tree's per-repo directories are independent clones (their own .git,
// their own refs, origin pointing at the canonical remote), materialized
// through the checkout mechanism's clone path with the machine cache as an
// accelerator. Worktrees are gone: no registration in the parent, no
// shared branch namespace, no back-reference that a parent move breaks.

#[test]
fn test_tree_add_creates_independent_clones() {
    let ws = WorkspaceBuilder::new().add_repo("app").add_repo("lib").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/clones")
        .expect("tree add should succeed");

    let tree_path = ws.workspace_root.parent().unwrap().join("feat-clones");

    // Each tree repo is a real clone: .git is a DIRECTORY (a worktree's .git
    // is a file pointing back at the parent's .git/worktrees/<name>).
    for name in ["app", "lib"] {
        let dotgit = tree_path.join(name).join(".git");
        assert!(
            dotgit.is_dir(),
            "tree repo {} must be an independent clone (.git dir), got {:?}",
            name,
            dotgit
        );
    }

    // The parent has no registered worktrees of the tree's names.
    let out = Command::new("git")
        .args(["-C", ws.repo_path("app").to_str().unwrap(), "worktree", "list", "--porcelain"])
        .output()
        .expect("git worktree list should run");
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        !text.contains("feat-clones"),
        "parent must hold no worktree registration for the tree: {}",
        text
    );
}

#[test]
fn test_tree_branch_is_isolated_from_parent_and_other_trees() {
    let ws = WorkspaceBuilder::new().add_repo("app").add_repo("lib").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/one")
        .expect("first tree add should succeed");
    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/two")
        .expect("second tree add should succeed");

    let root = ws.workspace_root.parent().unwrap();
    let one = root.join("feat-one").join("app");
    let two = root.join("feat-two").join("app");

    // Commit on a branch that exists only in tree one.
    git_helpers::create_branch(&one, "feat/one-exclusive");
    git_helpers::commit_file(&one, "one.txt", "one", "Add one.txt");

    // Not visible in the parent.
    assert!(
        !git_helpers::branch_exists(&ws.repo_path("app"), "feat/one-exclusive"),
        "parent must not see a branch created inside a tree clone"
    );
    // Not visible in the other tree.
    assert!(
        !git_helpers::branch_exists(&two, "feat/one-exclusive"),
        "tree two must not see tree one's branch"
    );
    // The parent's HEAD is untouched by tree commits.
    assertions::assert_on_branch(&ws.repo_path("app"), "main");
}

#[test]
fn test_tree_clone_origin_points_at_canonical_remote() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/origin")
        .expect("tree add should succeed");

    let tree_app = ws
        .workspace_root
        .parent()
        .unwrap()
        .join("feat-origin")
        .join("app");

    // The clone's origin must be the CANONICAL remote (the same bare the
    // parent was cloned from), not a path into the parent workspace.
    let out = Command::new("git")
        .args([
            "-C",
            tree_app.to_str().unwrap(),
            "remote",
            "get-url",
            "origin",
        ])
        .output()
        .expect("git remote get-url should run");
    let origin = String::from_utf8_lossy(&out.stdout).trim().to_string();
    assert_eq!(
        origin,
        ws.remote_url("app"),
        "tree clone origin must point at the canonical remote"
    );
}

#[test]
fn test_tree_survives_parent_rename() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/moved")
        .expect("tree add should succeed");

    // Rename the parent workspace directory (the worktree failure mode:
    // .git/worktrees back-references die with the move).
    let temp = &ws._temp;
    let old_ws = temp.path().join("workspace");
    let new_ws = temp.path().join("workspace-moved");
    fs::rename(&old_ws, &new_ws).expect("renaming parent workspace should work");

    // The tree's clone is an independent repo: HEAD, branches, and status
    // all work with the parent gone from its old address.
    let out = Command::new("git")
        .args([
            "-C",
            temp.path()
                .join("feat-moved")
                .join("app")
                .to_str()
                .unwrap(),
            "rev-parse",
            "--is-inside-work-tree",
        ])
        .output()
        .expect("git status should run in the tree clone after parent rename");
    assert!(
        out.status.success(),
        "tree clone must survive parent rename; stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let out = Command::new("git")
        .args([
            "-C",
            temp.path().join("feat-moved").join("app").to_str().unwrap(),
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ])
        .output()
        .expect("git rev-parse should run");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        "feat/moved",
        "tree clone must still be on the griptree branch after parent rename"
    );
}

#[test]
fn test_tree_remove_leaves_the_cache() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // The cache is an accelerator populated by `gr cache bootstrap`, never
    // by tree add; populate it first, then add the tree on top of it.
    let cache = gitgrip::core::workspace_cache::resolve_cache_path(
        &ws.workspace_root,
        "app",
        &ws.remote_url("app"),
    )
    .expect("cache path should resolve");
    gitgrip::core::workspace_cache::bootstrap_cache(
        &ws.workspace_root,
        "app",
        &ws.remote_url("app"),
    )
    .expect("cache bootstrap should succeed");
    assert!(cache.exists(), "cache should exist after bootstrap");

    gitgrip::cli::commands::tree::run_tree_add(&ws.workspace_root, &manifest, "feat/cached")
        .expect("tree add should succeed");

    gitgrip::cli::commands::tree::run_tree_remove(&ws.workspace_root, "feat/cached", false)
        .expect("tree remove should succeed");

    assert!(
        cache.exists(),
        "removing a tree must not remove the machine cache"
    );
}

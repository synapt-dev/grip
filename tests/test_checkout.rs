//! Integration tests for the checkout command.

mod common;

use assert_cmd::Command;

use common::assertions::assert_on_branch;
use common::fixtures::WorkspaceBuilder;

#[test]
fn test_checkout_existing_branch() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create a branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/checkout-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Go back to main
    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();
    assert_on_branch(&ws.repo_path("frontend"), "main");
    assert_on_branch(&ws.repo_path("backend"), "main");

    // Checkout the feature branch
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/checkout-test",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout should succeed: {:?}",
        result.err()
    );

    assert_on_branch(&ws.repo_path("frontend"), "feat/checkout-test");
    assert_on_branch(&ws.repo_path("backend"), "feat/checkout-test");
}

#[test]
fn test_checkout_nonexistent_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Checkout a branch that doesn't exist -- should succeed (skips repos)
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/does-not-exist",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout of nonexistent branch should not error: {:?}",
        result.err()
    );

    // Should still be on main
    assert_on_branch(&ws.repo_path("app"), "main");
}

#[test]
fn test_checkout_main() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let manifest = ws.load_manifest();

    // Create and switch to feature branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/temp"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();
    assert_on_branch(&ws.repo_path("app"), "feat/temp");

    // Checkout main
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout main should succeed: {:?}",
        result.err()
    );

    assert_on_branch(&ws.repo_path("app"), "main");
    assert_on_branch(&ws.repo_path("lib"), "main");
}

#[test]
fn test_checkout_create_flag() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Use -b flag to create and checkout in one command
    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/new-feature",
        true, // create = true (-b flag)
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout -b should succeed: {:?}",
        result.err()
    );

    // Both repos should now be on the new branch
    assert_on_branch(&ws.repo_path("frontend"), "feat/new-feature");
    assert_on_branch(&ws.repo_path("backend"), "feat/new-feature");
}
#[test]
fn test_checkout_skips_non_git_repo() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch across repos
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/checkout-safe"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        json: false,
    })
    .unwrap();

    // Corrupt backend repo by removing .git
    std::fs::remove_dir_all(ws.repo_path("backend").join(".git")).unwrap();

    let result = gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "feat/checkout-safe",
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "checkout should not crash on non-git repo: {:?}",
        result.err()
    );

    // Healthy repo should switch; corrupted repo remains non-git
    assert_on_branch(&ws.repo_path("frontend"), "feat/checkout-safe");
    assert!(!ws.repo_path("backend").join(".git").exists());
}

// ── grip#774: `gr checkout add` must produce a self-discoverable workspace ──
//
// grip#770/#771/#773 fixed `gr pr edit/review/merge` silently fanning out
// across every repo in the *correctly resolved* workspace. This is the sibling
// bug one layer down: `gr checkout add` materializes a disposable child
// checkout, but (before this fix) wrote no workspace marker inside it, so
// EVERY `gr` command run from inside that checkout -- including `gr pr
// review`/`merge` -- silently resolved the *parent* gripspace instead. A
// reviewer entering the checkout to act on its PR head could unknowingly
// operate on the parent's unrelated active branch. Found live during
// grip#773's own review/merge (grip#774).

#[test]
fn test_checkout_add_makes_the_child_checkout_independently_discoverable() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::checkout::run_checkout_add(
        &ws.workspace_root,
        &manifest,
        "review-copy",
        None,
        None,
    )
    .expect("checkout add should succeed");

    let checkout_repo_dir = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("review-copy")
        .join("app");
    assert!(
        checkout_repo_dir.is_dir(),
        "materialized checkout repo dir should exist at {}",
        checkout_repo_dir.display()
    );

    // This is Sentinel's exact grip#774 repro: run `gr env` from inside a
    // repo INSIDE the child checkout and check which workspace it reports.
    let output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(&checkout_repo_dir)
        .arg("env")
        .output()
        .expect("gr env should run");
    assert!(
        output.status.success(),
        "gr env should succeed from inside the checkout: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let stdout = String::from_utf8_lossy(&output.stdout);
    let workspace_line = stdout
        .lines()
        .find(|line| line.trim_start().starts_with("GITGRIP_WORKSPACE="))
        .expect("gr env should print GITGRIP_WORKSPACE");
    let reported = workspace_line
        .split_once('=')
        .map(|(_, v)| v.trim())
        .unwrap_or("");

    let expected_checkout_root = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("review-copy");
    let canonical_checkout_root =
        std::fs::canonicalize(&expected_checkout_root).unwrap_or(expected_checkout_root);
    let canonical_reported =
        std::fs::canonicalize(reported).unwrap_or_else(|_| std::path::PathBuf::from(reported));

    assert_eq!(
        canonical_reported, canonical_checkout_root,
        "gr env from inside the child checkout must resolve GITGRIP_WORKSPACE to the \
         checkout root, not the parent workspace -- that is grip#774's exact failure mode"
    );

    let canonical_parent =
        std::fs::canonicalize(&ws.workspace_root).unwrap_or_else(|_| ws.workspace_root.clone());
    assert_ne!(
        canonical_reported, canonical_parent,
        "gr env must not resolve to the parent workspace root from inside the checkout"
    );
}

// ── grip#775 blocker 1: a farther `.griptree` must not eclipse a nearer
// checkout. Reproduces Sentinel's exact live finding: his real parent
// gripspace carries a `.griptree` pointer, so `load_gripspace()`'s OLD
// two-independent-passes structure (check every ancestor for `.griptree`,
// THEN separately check every ancestor for `.gitgrip`) let that distant
// pointer win over the checkout one level down every time. ─────────────────

#[test]
fn test_checkout_wins_over_a_griptree_pointer_at_an_ancestor() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Simulate "this parent gripspace also happens to carry a .griptree
    // pointer" -- e.g. it was itself created as a griptree at some point.
    // The pointer's target doesn't need to resolve to anything real: this
    // test asserts the checkout wins BEFORE that pointer is ever followed.
    let pointer = gitgrip::core::griptree::GriptreePointer {
        main_workspace: "/nonexistent/decoy-main-workspace".to_string(),
        branch: "feat/decoy".to_string(),
        locked: false,
        created_at: None,
        repos: vec![],
        manifest_branch: None,
        manifest_worktree_name: None,
    };
    let pointer_json = serde_json::to_string(&pointer).expect("serialize pointer");
    std::fs::write(ws.workspace_root.join(".griptree"), pointer_json)
        .expect("write .griptree pointer at the parent gripspace root");

    gitgrip::cli::commands::checkout::run_checkout_add(
        &ws.workspace_root,
        &manifest,
        "review-copy",
        None,
        None,
    )
    .expect("checkout add should succeed even with a parent .griptree present");

    let checkout_repo_dir = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("review-copy")
        .join("app");

    let output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(&checkout_repo_dir)
        .arg("env")
        .output()
        .expect("gr env should run");
    assert!(
        output.status.success(),
        "gr env should succeed from inside the checkout: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let stdout = String::from_utf8_lossy(&output.stdout);
    let workspace_line = stdout
        .lines()
        .find(|line| line.trim_start().starts_with("GITGRIP_WORKSPACE="))
        .expect("gr env should print GITGRIP_WORKSPACE");
    let reported = workspace_line
        .split_once('=')
        .map(|(_, v)| v.trim())
        .unwrap_or("");

    let expected_checkout_root = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("review-copy");
    let canonical_checkout_root =
        std::fs::canonicalize(&expected_checkout_root).unwrap_or(expected_checkout_root);
    let canonical_reported =
        std::fs::canonicalize(reported).unwrap_or_else(|_| std::path::PathBuf::from(reported));

    assert_eq!(
        canonical_reported, canonical_checkout_root,
        "the nearer checkout must win over the farther .griptree pointer at the parent \
         gripspace root -- grip#775 blocker 1's exact failure mode"
    );
    assert_ne!(
        reported, "/nonexistent/decoy-main-workspace",
        "the griptree pointer must never even be followed when a nearer checkout exists"
    );
}

// ── grip#775 blocker 2: creating a checkout that includes the "manifest"
// pseudo-repo must not corrupt that repo's own materialized clone. ─────────

#[test]
fn test_checkout_including_manifest_repo_leaves_it_clean_end_to_end() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .with_manifest_repo()
        .build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::checkout::run_checkout_add(
        &ws.workspace_root,
        &manifest,
        "with-manifest",
        Some(&["app".to_string(), "manifest".to_string()]),
        None,
    )
    .expect("checkout add including the manifest repo should succeed");

    let checkout_root = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("with-manifest");
    let materialized_manifest_dir = checkout_root.join(".gitgrip").join("spaces").join("main");
    assert!(
        materialized_manifest_dir.join(".git").is_dir(),
        "the manifest repo should be materialized at its canonical clone path"
    );

    let status = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(&materialized_manifest_dir)
        .output()
        .expect("git status");
    assert!(
        status.stdout.is_empty(),
        "materialized manifest repo must be born clean, not carry a destructive derived-\
         manifest diff -- grip#775 blocker 2's exact failure mode. git status --porcelain: {}",
        String::from_utf8_lossy(&status.stdout)
    );

    // And discovery still works correctly from inside the OTHER materialized
    // repo in the same checkout -- proving .checkout.json (not anything
    // written into the manifest clone) is what makes this checkout resolvable.
    let output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(checkout_root.join("app"))
        .arg("env")
        .output()
        .expect("gr env should run");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let reported = stdout
        .lines()
        .find(|line| line.trim_start().starts_with("GITGRIP_WORKSPACE="))
        .and_then(|line| line.split_once('='))
        .map(|(_, v)| v.trim())
        .unwrap_or("");
    let canonical_checkout_root = std::fs::canonicalize(&checkout_root).unwrap_or(checkout_root);
    let canonical_reported =
        std::fs::canonicalize(reported).unwrap_or_else(|_| std::path::PathBuf::from(reported));
    assert_eq!(canonical_reported, canonical_checkout_root);
}

// ── grip#775 round 3: malformed checkout metadata must fail closed, never
// fail open to a parent workspace -- especially a parent that is itself a
// griptree, Sentinel's exact real-gripspace repro shape. ────────────────────

#[test]
fn test_corrupted_checkout_metadata_fails_closed_not_open_to_parent() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Match Sentinel's exact repro context: the parent also carries a
    // .griptree pointer, which is precisely the path a fail-open bug would
    // silently resolve to once the (corrupted) nearer checkout marker is
    // wrongly treated as absent.
    let pointer = gitgrip::core::griptree::GriptreePointer {
        main_workspace: "/nonexistent/decoy-main-workspace".to_string(),
        branch: "feat/decoy".to_string(),
        locked: false,
        created_at: None,
        repos: vec![],
        manifest_branch: None,
        manifest_worktree_name: None,
    };
    std::fs::write(
        ws.workspace_root.join(".griptree"),
        serde_json::to_string(&pointer).expect("serialize pointer"),
    )
    .expect("write .griptree pointer at the parent gripspace root");

    gitgrip::cli::commands::checkout::run_checkout_add(
        &ws.workspace_root,
        &manifest,
        "sentinel-repro-nomanifest",
        Some(&["app".to_string()]),
        None,
    )
    .expect("checkout add should succeed with no manifest repo included");

    let checkout_repo_dir = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("sentinel-repro-nomanifest")
        .join("app");

    // Sanity: gr env correctly reports the child BEFORE corruption (mirrors
    // step 2 of Sentinel's repro).
    let good_output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(&checkout_repo_dir)
        .arg("env")
        .output()
        .expect("gr env should run");
    assert!(
        good_output.status.success(),
        "gr env should succeed before corruption: {}",
        String::from_utf8_lossy(&good_output.stderr)
    );

    // Corrupt only the child-root .checkout.json (step 3).
    let checkout_root = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("sentinel-repro-nomanifest");
    std::fs::write(checkout_root.join(".checkout.json"), "not valid json{{{")
        .expect("corrupt checkout metadata");

    // Step 4: gr env again. Must NOT exit 0, and must NOT report the parent
    // workspace path -- that combination is grip#775 round 3's exact
    // reported failure mode.
    let corrupted_output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(&checkout_repo_dir)
        .arg("env")
        .output()
        .expect("gr env should run");

    assert!(
        !corrupted_output.status.success(),
        "gr env must fail (nonzero exit) when the nearest checkout marker exists but is \
         corrupted -- silently succeeding here means it fell through to some other \
         workspace instead of failing closed"
    );

    let stdout = String::from_utf8_lossy(&corrupted_output.stdout);
    let canonical_parent =
        std::fs::canonicalize(&ws.workspace_root).unwrap_or_else(|_| ws.workspace_root.clone());
    assert!(
        !stdout.contains(&canonical_parent.to_string_lossy().to_string())
            && !stdout.contains(&ws.workspace_root.to_string_lossy().to_string()),
        "gr env's output must not emit the parent workspace path at all when the nearer \
         checkout marker is corrupted -- got stdout: {}",
        stdout
    );

    let stderr = String::from_utf8_lossy(&corrupted_output.stderr);
    assert!(
        !stderr.is_empty(),
        "a failed gr env should explain why, not fail silently"
    );
}

// ── grip#775 round 4: an absolute or escaping repo path inside otherwise-
// valid checkout metadata must not redirect authority-bearing commands
// outside the checkout, e.g. onto an unrelated parent repo. ────────────────

#[test]
fn test_absolute_repo_path_in_metadata_cannot_escape_the_checkout() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    let pointer = gitgrip::core::griptree::GriptreePointer {
        main_workspace: "/nonexistent/decoy-main-workspace".to_string(),
        branch: "feat/decoy".to_string(),
        locked: false,
        created_at: None,
        repos: vec![],
        manifest_branch: None,
        manifest_worktree_name: None,
    };
    std::fs::write(
        ws.workspace_root.join(".griptree"),
        serde_json::to_string(&pointer).expect("serialize pointer"),
    )
    .expect("write .griptree pointer at the parent gripspace root");

    gitgrip::cli::commands::checkout::run_checkout_add(
        &ws.workspace_root,
        &manifest,
        "sentinel-repro-escape",
        Some(&["app".to_string()]),
        None,
    )
    .expect("checkout add should succeed");

    let checkout_root = ws
        .workspace_root
        .join(".grip")
        .join("checkouts")
        .join("sentinel-repro-escape");
    let checkout_repo_dir = checkout_root.join("app");

    // Sanity: works correctly before corruption.
    let good_output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(&checkout_repo_dir)
        .arg("env")
        .output()
        .expect("gr env should run");
    assert!(
        good_output.status.success(),
        "gr env should succeed before the metadata is tampered with"
    );

    // Rewrite .checkout.json's "app" entry to an ABSOLUTE relative_path
    // pointing at the PARENT's own real "app" repo -- otherwise-valid,
    // nonempty metadata, exactly Sentinel's repro shape. PathBuf::join
    // silently discards the checkout-root base when the joined path is
    // absolute, so an unvalidated reconstruction would resolve authority-
    // bearing commands onto this unrelated parent repo instead.
    let meta_path = checkout_root.join(".checkout.json");
    let raw = std::fs::read_to_string(&meta_path).expect("read checkout metadata");
    let mut value: serde_json::Value = serde_json::from_str(&raw).expect("parse metadata");
    let parent_app_path = ws.workspace_root.join("app");
    value["repos"][0]["relative_path"] =
        serde_json::Value::String(parent_app_path.to_string_lossy().to_string());
    std::fs::write(&meta_path, serde_json::to_string_pretty(&value).unwrap())
        .expect("write tampered metadata");

    let escaped_output = Command::cargo_bin("gr")
        .expect("gr binary should build")
        .current_dir(&checkout_repo_dir)
        .arg("env")
        .output()
        .expect("gr env should run");

    assert!(
        !escaped_output.status.success(),
        "gr env must reject a reconstructed manifest whose repo path is absolute -- silently \
         succeeding means an authority-bearing command could resolve outside the checkout, \
         grip#775 round 4's exact scope escape"
    );

    let stdout = String::from_utf8_lossy(&escaped_output.stdout);
    assert!(
        !stdout.contains(&parent_app_path.to_string_lossy().to_string()),
        "the rejected reconstruction must never surface the escaping absolute path in output: {}",
        stdout
    );

    let stderr = String::from_utf8_lossy(&escaped_output.stderr);
    assert!(
        !stderr.is_empty(),
        "a rejected reconstruction should explain why, not fail silently"
    );
}

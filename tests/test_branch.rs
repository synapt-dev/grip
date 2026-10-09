//! Integration tests for the branch command.

mod common;

use common::assertions::{assert_branch_exists, assert_branch_not_exists, assert_on_branch};
use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_branch_create_across_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/new-feature"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch create should succeed: {:?}",
        result.err()
    );

    // Both repos should now be on the new branch
    assert_on_branch(&ws.repo_path("frontend"), "feat/new-feature");
    assert_on_branch(&ws.repo_path("backend"), "feat/new-feature");
}

#[test]
fn test_branch_delete() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch first
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/to-delete"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    // Switch back to main so we can delete
    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();

    // Delete the branch
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/to-delete"),
            delete: true,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch delete should succeed: {:?}",
        result.err()
    );

    assert_branch_not_exists(&ws.repo_path("frontend"), "feat/to-delete");
    assert_branch_not_exists(&ws.repo_path("backend"), "feat/to-delete");
}

#[test]
fn test_branch_list() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create a couple branches
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/one"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();
    git_helpers::checkout(&ws.repo_path("app"), "main");
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/two"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    // List branches (no name passed)
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: None,
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch list should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_filter_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .add_repo("shared")
        .build();

    let manifest = ws.load_manifest();

    // Create branch only in frontend and backend
    let filter = vec!["frontend".to_string(), "backend".to_string()];
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/filtered"),
            delete: false,
            move_commits: false,
            repos_filter: Some(&filter),
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "filtered branch should succeed: {:?}",
        result.err()
    );

    assert_on_branch(&ws.repo_path("frontend"), "feat/filtered");
    assert_on_branch(&ws.repo_path("backend"), "feat/filtered");
    // shared should still be on main
    assert_on_branch(&ws.repo_path("shared"), "main");
}

#[test]
fn test_branch_skip_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/skip-refs"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(result.is_ok(), "branch should succeed: {:?}", result.err());

    // app should be on the new branch
    assert_on_branch(&ws.repo_path("app"), "feat/skip-refs");
    // docs (reference) should still be on main
    assert_on_branch(&ws.repo_path("docs"), "main");
}

#[test]
fn test_branch_idempotent_creation() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create branch
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/existing"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    // Create same branch again -- should not error (prints "already exists")
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/existing"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "creating an existing branch should not fail: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_not_cloned_repo() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    // Manually remove the cloned repo to simulate "not cloned"
    std::fs::remove_dir_all(ws.repo_path("app")).unwrap();

    let manifest = ws.load_manifest();

    // Should succeed (prints warning for not-cloned repo)
    let result =
        gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
            workspace_root: &ws.workspace_root,
            manifest: &manifest,
            name: Some("feat/no-repo"),
            delete: false,
            move_commits: false,
            repos_filter: None,
            group_filter: None,
            include_parked: false,
            json: false,
        });
    assert!(
        result.is_ok(),
        "branch on missing repo should not fail: {:?}",
        result.err()
    );
}

#[test]
fn test_branch_create_then_verify_branches_exist() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();

    let manifest = ws.load_manifest();

    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/verify"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    assert_branch_exists(&ws.repo_path("alpha"), "feat/verify");
    assert_branch_exists(&ws.repo_path("beta"), "feat/verify");
    // main should still exist too
    assert_branch_exists(&ws.repo_path("alpha"), "main");
    assert_branch_exists(&ws.repo_path("beta"), "main");
}

/// Regression test for grip#401: `gr branch` on an existing branch must
/// switch to it so subsequent commits land on the correct branch.
#[test]
fn test_branch_switches_to_existing_branch() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let manifest = ws.load_manifest();

    // Create feat/target and then switch back to main
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/target"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    gitgrip::cli::commands::checkout::run_checkout(
        &ws.workspace_root,
        &manifest,
        "main",
        false,
        None,
        None,
    )
    .unwrap();
    assert_on_branch(&ws.repo_path("app"), "main");

    // Run `gr branch feat/target` again — should switch to it
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/target"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    // Must be on feat/target, not main
    assert_on_branch(&ws.repo_path("app"), "feat/target");
}

// ---- a workspace-wide `gr branch` leaves a PARKED clone where it is, and says so -----------------------------
//
// One `gr branch feat/x` with no --repo used to switch every clone, including ones parked on other work and ones that had to
// stay on their default branch. A clone not on the branch the workspace expects (the manifest revision, or inside a griptree
// the tree's own branch) is now skipped and named; `--repo` names a clone, `--include-parked` takes them all.

fn run_create(
    root: &std::path::PathBuf,
    manifest: &gitgrip::core::manifest::Manifest,
    name: &str,
    repos: Option<&[String]>,
    include_parked: bool,
    move_commits: bool,
) -> anyhow::Result<()> {
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: root,
        manifest,
        name: Some(name),
        delete: false,
        move_commits,
        repos_filter: repos,
        group_filter: None,
        include_parked,
        json: false,
    })
}

#[test]
fn test_branch_leaves_a_parked_clone_where_it_is() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/wip");
    let parked_head = git_helpers::get_head_sha(&ws.repo_path("beta"));

    run_create(&ws.workspace_root, &manifest, "feat/x", None, false, false).unwrap();

    assert_on_branch(&ws.repo_path("alpha"), "feat/x");
    assert_on_branch(&ws.repo_path("beta"), "hold/wip");
    assert_branch_not_exists(&ws.repo_path("beta"), "feat/x");
    assert_eq!(
        git_helpers::get_head_sha(&ws.repo_path("beta")),
        parked_head
    );
}

#[test]
fn test_branch_include_parked_takes_every_clone() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/wip");
    let parked_head = git_helpers::get_head_sha(&ws.repo_path("beta"));

    run_create(&ws.workspace_root, &manifest, "feat/x", None, true, false).unwrap();

    assert_on_branch(&ws.repo_path("alpha"), "feat/x");
    assert_on_branch(&ws.repo_path("beta"), "feat/x");
    // cut at the parked tip, which is what the old behaviour did to a parked clone
    assert_eq!(
        git_helpers::get_head_sha(&ws.repo_path("beta")),
        parked_head
    );
}

#[test]
fn test_branch_repo_flag_names_a_parked_clone() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/wip");

    run_create(
        &ws.workspace_root,
        &manifest,
        "feat/x",
        Some(&["beta".to_string()]),
        false,
        false,
    )
    .unwrap();

    assert_on_branch(&ws.repo_path("beta"), "feat/x");
    assert_on_branch(&ws.repo_path("alpha"), "main");
}

#[test]
fn test_branch_rerun_is_not_parked_by_being_on_the_target() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();

    run_create(&ws.workspace_root, &manifest, "feat/x", None, false, false).unwrap();
    // every clone is now off its default branch, but ON the target: a second run is a no-op switch, not a skip
    run_create(&ws.workspace_root, &manifest, "feat/x", None, false, false).unwrap();

    assert_on_branch(&ws.repo_path("alpha"), "feat/x");
    assert_on_branch(&ws.repo_path("beta"), "feat/x");

    // the branch state alone cannot tell "switched" from "skipped as parked" here (a parked clone stays on
    // feat/x too), so the report is what pins it
    let out = assert_cmd::Command::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .args(["--json", "branch", "feat/x"])
        .output()
        .unwrap();
    let rows: serde_json::Value = serde_json::from_slice(&out.stdout).expect("--json prints JSON");
    for row in rows.as_array().expect("an array of per-clone results") {
        assert_eq!(row["action"], "switched", "{row}");
        assert_eq!(row["from_branch"], "feat/x", "{row}");
    }
}

#[test]
fn test_branch_detached_head_counts_as_parked() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();
    let sha = git_helpers::get_head_sha(&ws.repo_path("beta"));
    git_helpers::checkout(&ws.repo_path("beta"), &sha);

    run_create(&ws.workspace_root, &manifest, "feat/x", None, false, false).unwrap();

    assert_on_branch(&ws.repo_path("alpha"), "feat/x");
    assert_branch_not_exists(&ws.repo_path("beta"), "feat/x");
}

#[test]
fn test_branch_in_a_griptree_expects_the_tree_branch() {
    use gitgrip::core::griptree::GriptreeConfig;

    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();
    // inside a tree every clone sits on the tree's branch on purpose
    git_helpers::create_branch(&ws.repo_path("alpha"), "feat/tree");
    git_helpers::create_branch(&ws.repo_path("beta"), "feat/tree");

    // no griptree config: both are off the manifest revision, so both are parked and nothing moves
    assert!(run_create(&ws.workspace_root, &manifest, "feat/y", None, false, false).is_err());
    assert_on_branch(&ws.repo_path("alpha"), "feat/tree");
    assert_on_branch(&ws.repo_path("beta"), "feat/tree");

    // with the tree's config the same state is the EXPECTED one, so both move
    let mut config = GriptreeConfig::new("feat/tree", &ws.workspace_root.to_string_lossy());
    config
        .save(&ws.workspace_root.join(".gitgrip").join("griptree.json"))
        .unwrap();
    run_create(&ws.workspace_root, &manifest, "feat/y", None, false, false).unwrap();
    assert_on_branch(&ws.repo_path("alpha"), "feat/y");
    assert_on_branch(&ws.repo_path("beta"), "feat/y");
}

#[test]
fn test_branch_move_leaves_a_parked_clone_alone() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    let manifest = ws.load_manifest();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/wip");
    git_helpers::commit_file(
        &ws.repo_path("beta"),
        "wip.txt",
        "parked work",
        "parked work",
    );
    let parked_head = git_helpers::get_head_sha(&ws.repo_path("beta"));

    // `--move` hard-resets the CURRENT branch to its remote: it must never reach a clone parked on other work
    run_create(&ws.workspace_root, &manifest, "feat/m", None, false, true).unwrap();

    assert_on_branch(&ws.repo_path("beta"), "hold/wip");
    assert_branch_not_exists(&ws.repo_path("beta"), "feat/m");
    assert_eq!(
        git_helpers::get_head_sha(&ws.repo_path("beta")),
        parked_head
    );
}

#[test]
fn test_branch_json_reports_the_parked_clone_and_where_each_clone_was() {
    use assert_cmd::Command;

    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/wip");

    let out = Command::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .args(["--json", "branch", "feat/x"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "a skipped parked clone is the designed outcome, exit 0"
    );

    let rows: serde_json::Value = serde_json::from_slice(&out.stdout).expect("--json prints JSON");
    let rows = rows.as_array().expect("an array of per-clone results");
    let find = |name: &str| {
        rows.iter()
            .find(|r| r["repo"] == name)
            .unwrap_or_else(|| panic!("no row for {name}"))
    };
    assert_eq!(find("alpha")["action"], "created");
    assert_eq!(find("alpha")["from_branch"], "main");
    assert_eq!(find("beta")["action"], "parked");
    assert_eq!(find("beta")["from_branch"], "hold/wip");
    assert_eq!(find("beta")["expected_branch"], "main");
}

// ---- the report is half the behaviour: these rows run the real binary and read what it prints --------------------------
//
// The rows above call `run_branch` as a library, so nothing in them can see the closing line, the per-clone text, a group
// filter, or the --move JSON. Each row below drives `gr` itself.

fn gr(ws: &std::path::Path, args: &[&str]) -> (i32, String) {
    let (code, out, _err) = gr_full(ws, args);
    (code, out)
}

fn gr_full(ws: &std::path::Path, args: &[&str]) -> (i32, String, String) {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_gr1"))
        .args(args)
        .current_dir(ws)
        .env("HOME", ws.parent().unwrap())
        .env("NO_COLOR", "1")
        .output()
        .unwrap();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

#[test]
fn test_branch_report_counts_what_moved_and_names_what_was_skipped() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .add_repo("gamma")
        .build();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");
    // gamma already has the branch, so it SWITCHES instead of creating
    git_helpers::create_branch(&ws.repo_path("gamma"), "feat/x");
    git_helpers::checkout(&ws.repo_path("gamma"), "main");

    let (code, out) = gr(&ws.workspace_root, &["branch", "feat/x"]);

    assert_eq!(code, 0, "{out}");
    assert!(out.contains("alpha: main -> feat/x (created)"), "{out}");
    assert!(
        out.contains("gamma: main -> feat/x (already exists, switched)"),
        "{out}"
    );
    assert!(
        out.contains("beta: parked (on hold/beta, expected main), left where it was"),
        "{out}"
    );
    // alpha is created and gamma is switched, so two moved; beta is the one skipped
    assert!(out.contains("2 moved, 1 skipped (parked): beta"), "{out}");
}

#[test]
fn test_branch_move_report_counts_what_moved_and_names_what_was_skipped() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");

    let (code, out) = gr(&ws.workspace_root, &["branch", "feat/m", "--move"]);

    assert_eq!(code, 0, "{out}");
    assert!(
        out.contains("beta: parked (on hold/beta, expected main), left where it was"),
        "{out}"
    );
    assert!(
        out.contains("alpha: moved commits from main to feat/m"),
        "{out}"
    );
    assert!(out.contains("1 moved, 1 skipped (parked): beta"), "{out}");
    assert_on_branch(&ws.repo_path("beta"), "hold/beta");
}

#[test]
fn test_branch_group_filter_is_still_workspace_wide() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("alpha", vec!["docs"])
        .add_repo_with_groups("beta", vec!["docs"])
        .add_repo_with_groups("gamma", vec!["other"])
        .build();
    git_helpers::create_branch(&ws.repo_path("alpha"), "hold/alpha");

    let (code, out) = gr(&ws.workspace_root, &["branch", "feat/z", "--group", "docs"]);

    assert_eq!(code, 0, "{out}");
    // a group names a set of clones, not a single clone: the parked one in the group is still skipped
    assert_on_branch(&ws.repo_path("alpha"), "hold/alpha");
    assert_on_branch(&ws.repo_path("beta"), "feat/z");
    assert_on_branch(&ws.repo_path("gamma"), "main");
}

#[test]
fn test_branch_move_json_reports_the_parked_clone() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");

    let (code, out) = gr(
        &ws.workspace_root,
        &["--json", "branch", "feat/m", "--move"],
    );

    assert_eq!(code, 0, "{out}");
    let rows: serde_json::Value = serde_json::from_str(&out).expect("--json prints JSON");
    let beta = rows
        .as_array()
        .expect("an array of per-clone results")
        .iter()
        .find(|r| r["repo"] == "beta")
        .expect("a row for beta");
    assert_eq!(beta["action"], "parked", "{beta}");
    assert_eq!(beta["from_branch"], "hold/beta", "{beta}");
    assert_eq!(beta["expected_branch"], "main", "{beta}");
}

#[test]
fn test_branch_unreadable_current_branch_is_an_error_row_not_a_blank_name() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    // an orphan branch has no commit yet, so HEAD cannot be read as a branch tip
    let status = std::process::Command::new("git")
        .args(["checkout", "-q", "--orphan", "unborn"])
        .current_dir(ws.repo_path("beta"))
        .status()
        .unwrap();
    assert!(status.success());

    let (_code, out) = gr(&ws.workspace_root, &["--json", "branch", "feat/x"]);

    let rows: serde_json::Value = serde_json::from_str(&out).expect("--json prints JSON");
    let beta = rows
        .as_array()
        .expect("an array of per-clone results")
        .iter()
        .find(|r| r["repo"] == "beta")
        .expect("a row for beta");
    assert_eq!(beta["action"], "error", "{beta}");
    assert!(
        beta["error"]
            .as_str()
            .unwrap_or("")
            .contains("failed to get current branch"),
        "{beta}"
    );
}

// ---- a run that moved nothing while skipping clones fails, so `gr branch x && gr commit` stops ----------------------------

#[test]
fn test_branch_exits_1_when_every_clone_is_parked() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("alpha"), "hold/alpha");
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");

    let (code, out, err) = gr_full(&ws.workspace_root, &["branch", "feat/x"]);

    assert_eq!(code, 1, "{out}{err}");
    assert!(out.contains("0 moved, 2 skipped (parked): "), "{out}");
    assert!(err.contains("no clone moved to 'feat/x'"), "{err}");
    assert!(
        err.contains("--repo") && err.contains("--include-parked"),
        "{err}"
    );
    assert_on_branch(&ws.repo_path("alpha"), "hold/alpha");
    assert_on_branch(&ws.repo_path("beta"), "hold/beta");
}

#[test]
fn test_branch_move_exits_1_when_every_clone_is_parked() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("alpha"), "hold/alpha");
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");

    let (code, out, err) = gr_full(&ws.workspace_root, &["branch", "feat/m", "--move"]);

    assert_eq!(code, 1, "{out}{err}");
    assert!(out.contains("0 moved, 2 skipped (parked): "), "{out}");
}

#[test]
fn test_branch_json_exits_1_when_every_clone_is_parked() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("alpha"), "hold/alpha");
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");

    let (code, out, _err) = gr_full(&ws.workspace_root, &["--json", "branch", "feat/x"]);

    // a script gets the rows AND the failing exit status
    assert_eq!(code, 1, "{out}");
    let rows: serde_json::Value = serde_json::from_str(&out).expect("--json prints JSON");
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .all(|r| r["action"] == "parked"),
        "{out}"
    );
}

#[test]
fn test_branch_exits_0_when_everything_is_already_on_the_target() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    assert_eq!(gr(&ws.workspace_root, &["branch", "feat/x"]).0, 0);

    // every clone now sits on the target: a re-run switches them (nothing is skipped), so it is a success
    let (code, out) = gr(&ws.workspace_root, &["branch", "feat/x"]);

    assert_eq!(code, 0, "{out}");
    assert!(!out.contains("skipped (parked)"), "{out}");
}

#[test]
fn test_branch_include_parked_exits_0_when_every_clone_is_parked() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();
    git_helpers::create_branch(&ws.repo_path("alpha"), "hold/alpha");
    git_helpers::create_branch(&ws.repo_path("beta"), "hold/beta");

    let (code, out) = gr(
        &ws.workspace_root,
        &["branch", "feat/x", "--include-parked"],
    );

    assert_eq!(code, 0, "{out}");
    assert_on_branch(&ws.repo_path("alpha"), "feat/x");
    assert_on_branch(&ws.repo_path("beta"), "feat/x");
}

//! CLI integration tests
//!
//! Tests the CLI binary end-to-end.

mod common;

use assert_cmd::Command;
use gitgrip::core::griptree::GriptreeConfig;
use predicates::prelude::*;
use tempfile::TempDir;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

/// Test that `gr --help` works
#[test]
fn test_help() {
    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.arg("--help")
        .assert()
        .success()
        .stdout(predicate::str::contains("Multi-repo workflow tool"));
}

/// Test that `gr --version` works
#[test]
fn test_version() {
    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::contains(env!("CARGO_PKG_VERSION")));
}

#[test]
fn test_checkout_help_mentions_add_mode() {
    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.arg("checkout")
        .arg("--help")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Checkout a branch across repos or manage independent child checkouts",
        ))
        .stdout(predicate::str::contains(
            "Branch name, or `add`/`list`/`remove` for child checkout lifecycle",
        ))
        .stdout(predicate::str::contains("gr checkout add sandbox"))
        .stdout(predicate::str::contains(
            "gr checkout add docs-only --group docs",
        ))
        .stdout(predicate::str::contains("gr checkout list"))
        .stdout(predicate::str::contains("gr checkout remove sandbox"));
}

/// Test that `gr status` fails gracefully outside a workspace
#[test]
fn test_status_outside_workspace() {
    let temp = TempDir::new().unwrap();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(temp.path())
        .arg("status")
        .assert()
        .failure()
        .stderr(predicate::str::contains("Not in a gitgrip workspace"));
}

/// Test that `gr bench --list` works
#[test]
fn test_bench_list() {
    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.arg("bench")
        .arg("--list")
        .assert()
        .success()
        .stdout(predicate::str::contains("Available Benchmarks"));
}

/// Test that `gr bench` runs benchmarks
#[test]
fn test_bench_run() {
    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.arg("bench")
        .arg("-n")
        .arg("1")
        .assert()
        .success()
        .stdout(predicate::str::contains("Benchmark Results"));
}

/// Test that `gr bench --json` outputs JSON
#[test]
fn test_bench_json() {
    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.arg("bench")
        .arg("-n")
        .arg("1")
        .arg("--json")
        .assert()
        .success()
        .stdout(predicate::str::starts_with("["));
}

#[test]
fn test_checkout_base_uses_griptree_config() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    git_helpers::create_branch(&ws.repo_path("app"), "feat/base");
    git_helpers::checkout(&ws.repo_path("app"), "main");
    git_helpers::create_branch(&ws.repo_path("lib"), "feat/base");
    git_helpers::checkout(&ws.repo_path("lib"), "main");

    let mut config = GriptreeConfig::new("feat/base", &ws.workspace_root.to_string_lossy());
    let config_path = ws.workspace_root.join(".gitgrip").join("griptree.json");
    config.save(&config_path).unwrap();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("--base")
        .assert()
        .success();

    assert_eq!(
        git_helpers::current_branch(&ws.repo_path("app")),
        "feat/base"
    );
    assert_eq!(
        git_helpers::current_branch(&ws.repo_path("lib")),
        "feat/base"
    );
}

#[test]
fn test_checkout_add_materializes_independent_child_checkout() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success()
        .stdout(predicate::str::contains("Created checkout 'sandbox'"));

    let checkout_root = ws.workspace_root.join(".grip/checkouts/sandbox");
    let app_checkout = checkout_root.join("app");
    let lib_checkout = checkout_root.join("lib");
    assert!(app_checkout.join(".git").is_dir());
    assert!(!app_checkout.join(".git").is_file());
    assert!(lib_checkout.join(".git").is_dir());
    assert!(!lib_checkout.join(".git").is_file());

    let origin = std::process::Command::new("git")
        .args(["remote", "get-url", "origin"])
        .current_dir(&app_checkout)
        .output()
        .expect("git remote get-url");
    let origin = String::from_utf8_lossy(&origin.stdout).trim().to_string();
    assert_eq!(origin, ws.remote_url("app"));
}

#[test]
fn test_checkout_add_respects_repo_filter() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("app-only")
        .arg("--repo")
        .arg("app")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Created checkout 'app-only' with 1 repo(s)",
        ));

    let checkout_root = ws.workspace_root.join(".grip/checkouts/app-only");
    assert!(checkout_root.join("app/.git").is_dir());
    assert!(!checkout_root.join("lib").exists());
}

#[test]
fn test_checkout_add_respects_group_filter() {
    let ws = WorkspaceBuilder::new()
        .add_repo_with_groups("app", vec!["product"])
        .add_repo_with_groups("docs", vec!["docs"])
        .build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("docs-only")
        .arg("--group")
        .arg("docs")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Created checkout 'docs-only' with 1 repo(s)",
        ));

    let checkout_root = ws.workspace_root.join(".grip/checkouts/docs-only");
    assert!(checkout_root.join("docs/.git").is_dir());
    assert!(!checkout_root.join("app").exists());
}

#[test]
fn test_checkout_add_requires_name() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "Checkout name is required: gr checkout add <name>",
        ));
}

#[test]
fn test_checkout_add_errors_when_filters_match_no_repos() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("empty")
        .arg("--repo")
        .arg("missing")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "repo filter 'missing' not found in local manifest",
        ));
}

#[test]
fn test_pr_merge_unknown_repo_is_a_process_level_refusal() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("pr")
        .arg("merge")
        .arg("--repo")
        .arg("missing")
        .arg("--method")
        .arg("merge")
        .arg("--yes")
        .assert()
        .code(2)
        .stderr(predicate::str::contains(
            "repo filter 'missing' not found in local manifest",
        ));
}

#[test]
fn test_pr_merge_known_repo_with_no_open_pr_is_still_success() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("pr")
        .arg("merge")
        .arg("--repo")
        .arg("app")
        .arg("--method")
        .arg("merge")
        .arg("--yes")
        .assert()
        .success()
        .stdout(predicate::str::contains("No open PRs found"))
        .stdout(predicate::str::contains("Repositories checked: 1"));
}

#[test]
fn test_checkout_add_rejects_create_and_base_flags() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut create_cmd = Command::cargo_bin("gr1").unwrap();
    create_cmd
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .arg("--create")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "--create and --base are not valid with 'add'",
        ));

    let mut base_cmd = Command::cargo_bin("gr1").unwrap();
    base_cmd
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .arg("--base")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "--create and --base are not valid with 'add'",
        ));
}

#[test]
fn test_checkout_add_rejects_extra_positional_args() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .arg("extra")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "unexpected extra arguments after checkout name",
        ));
}

#[test]
fn test_checkout_add_rejects_duplicate_checkout_name() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut first = Command::cargo_bin("gr1").unwrap();
    first
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr1").unwrap();
    duplicate
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "checkout 'sandbox' already exists",
        ));
}

#[test]
fn test_checkout_list_shows_materialized_checkouts() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut add = Command::cargo_bin("gr1").unwrap();
    add.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr1").unwrap();
    list.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("Checkouts"))
        .stdout(predicate::str::contains("sandbox ->"));
}

#[test]
fn test_checkout_list_reports_empty_state() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut list = Command::cargo_bin("gr1").unwrap();
    list.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("No checkouts configured."));
}

#[test]
fn test_checkout_list_rejects_extra_positional_args() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut list = Command::cargo_bin("gr1").unwrap();
    list.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("list")
        .arg("extra")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "`gr checkout list` does not accept extra arguments",
        ));
}

#[test]
fn test_checkout_remove_deletes_materialized_checkout() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let checkout_root = ws.workspace_root.join(".grip/checkouts/sandbox");

    let mut add = Command::cargo_bin("gr1").unwrap();
    add.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success();

    assert!(checkout_root.is_dir());

    let mut remove = Command::cargo_bin("gr1").unwrap();
    remove
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("remove")
        .arg("sandbox")
        .assert()
        .success()
        .stdout(predicate::str::contains("Removed checkout 'sandbox'"));

    assert!(!checkout_root.exists());
}

#[test]
fn test_checkout_remove_errors_for_missing_checkout() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut remove = Command::cargo_bin("gr1").unwrap();
    remove
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("remove")
        .arg("missing")
        .assert()
        .failure()
        .stderr(predicate::str::contains("Checkout 'missing' not found"));
}

#[test]
fn test_checkout_remove_rejects_extra_positional_args() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut remove = Command::cargo_bin("gr1").unwrap();
    remove
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("remove")
        .arg("sandbox")
        .arg("extra")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "unexpected extra arguments after checkout name",
        ));
}

// --- #196: repo-filter validation on the staging/commit/push verbs -------------
//
// `validate_repo_filters_known` already existed and already produced the right
// message, including a basename suggestion for the common mistake that surfaced
// this: a manifest entry named `<prefix>-<name>` checked out at `./<name>`, where
// the operator naturally types `--repo <name>`. These three verbs did not call
// the validator, so an unknown `--repo` name matched zero repos and the command
// reported success.
//
// The failure direction is what makes it worth a test: another agent reads
// "pushed" or "staged" and acts on it.

#[test]
fn test_add_unknown_repo_filter_is_refused_not_silently_empty() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("add")
        .arg(".")
        .arg("--repo")
        .arg("missing")
        .assert()
        .code(2)
        .stderr(predicate::str::contains(
            "repo filter 'missing' not found in local manifest",
        ));
}

#[test]
fn test_commit_unknown_repo_filter_is_refused_not_silently_empty() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("commit")
        .arg("-m")
        .arg("msg")
        .arg("--repo")
        .arg("missing")
        .assert()
        .code(2)
        .stderr(predicate::str::contains(
            "repo filter 'missing' not found in local manifest",
        ));
}

#[test]
fn test_push_unknown_repo_filter_is_refused_not_silently_empty() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("push")
        .arg("--repo")
        .arg("missing")
        .assert()
        .code(2)
        .stderr(predicate::str::contains(
            "repo filter 'missing' not found in local manifest",
        ));
}

/// Control for the three tests above: a KNOWN repo name must still REACH THE
/// WORK, so the rejections cannot be passing merely because `gr add` fails for
/// some unrelated reason in this fixture.
///
/// The control asserts the DESTINATION, not the absence of a message. An
/// earlier version of this test wrote no file and checked only that one
/// substring was missing from stderr — which passes identically whether `add`
/// stages the file or does nothing at all, and those are exactly the two
/// outcomes a control has to separate. The fixture commits its files before
/// cloning, so the worktree starts clean and the test must dirty it itself.
#[test]
fn test_add_known_repo_filter_reaches_the_work_and_stages() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();
    let repo = ws.repo_path("app");
    std::fs::write(repo.join("control.txt"), "dirty\n").unwrap();

    let mut cmd = Command::cargo_bin("gr1").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("add")
        .arg(".")
        .arg("--repo")
        .arg("app")
        .assert()
        .success()
        .stderr(predicate::str::contains("not found in local manifest").not());

    let staged = std::process::Command::new("git")
        .args(["diff", "--cached", "--name-only"])
        .current_dir(&repo)
        .output()
        .unwrap();
    let staged = String::from_utf8_lossy(&staged.stdout);
    assert!(
        staged.contains("control.txt"),
        "known --repo name must reach the work: expected control.txt in the index, got {staged:?}"
    );
}

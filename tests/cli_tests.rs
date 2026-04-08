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
    let mut cmd = Command::cargo_bin("gr").unwrap();
    cmd.arg("--help")
        .assert()
        .success()
        .stdout(predicate::str::contains("Multi-repo workflow tool"));
}

/// Test that `gr --version` works
#[test]
fn test_version() {
    let mut cmd = Command::cargo_bin("gr").unwrap();
    cmd.arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::contains(env!("CARGO_PKG_VERSION")));
}

#[test]
fn test_gr2_help() {
    let mut cmd = Command::cargo_bin("gr2").unwrap();
    cmd.arg("--help")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "gr2 is the clean-break gitgrip CLI for the new team-workspace, cache, and checkout model.",
        ))
        .stdout(predicate::str::contains("doctor"))
        .stdout(predicate::str::contains("gr2"));
}

#[test]
fn test_gr2_version() {
    let mut cmd = Command::cargo_bin("gr2").unwrap();
    cmd.arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::contains("gr2 0.1.0"));
}

#[test]
fn test_gr2_doctor() {
    let mut cmd = Command::cargo_bin("gr2").unwrap();
    cmd.arg("doctor")
        .assert()
        .success()
        .stdout(predicate::str::contains("gr2 bootstrap OK"));
}

#[test]
fn test_gr2_init_scaffolds_team_workspace() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut cmd = Command::cargo_bin("gr2").unwrap();
    cmd.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Initialized gr2 team workspace 'demo'",
        ));

    assert!(workspace_root.join(".grip").is_dir());
    assert!(workspace_root.join("config").is_dir());
    assert!(workspace_root.join("agents").is_dir());
    assert!(workspace_root.join("repos").is_dir());

    let workspace_toml =
        std::fs::read_to_string(workspace_root.join(".grip/workspace.toml")).unwrap();
    assert!(workspace_toml.contains("version = 2"));
    assert!(workspace_toml.contains("name = \"demo\""));
    assert!(workspace_toml.contains("layout = \"team-workspace\""));
}

#[test]
fn test_gr2_init_rejects_existing_path() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");
    std::fs::create_dir_all(&workspace_root).unwrap();

    let mut cmd = Command::cargo_bin("gr2").unwrap();
    cmd.arg("init")
        .arg(&workspace_root)
        .assert()
        .failure()
        .stderr(predicate::str::contains("workspace path already exists"));
}

#[test]
fn test_gr2_team_add_registers_agent_workspace() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut team_add = Command::cargo_bin("gr2").unwrap();
    team_add
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Added gr2 agent workspace 'atlas'",
        ));

    let agent_toml =
        std::fs::read_to_string(workspace_root.join("agents/atlas/agent.toml")).unwrap();
    assert!(agent_toml.contains("name = \"atlas\""));
    assert!(agent_toml.contains("kind = \"agent-workspace\""));
}

#[test]
fn test_gr2_team_add_rejects_duplicate_agent() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut first = Command::cargo_bin("gr2").unwrap();
    first
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr2").unwrap();
    duplicate
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .failure()
        .stderr(predicate::str::contains("agent 'atlas' already exists"));
}

#[test]
fn test_gr2_team_add_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut team_add = Command::cargo_bin("gr2").unwrap();
    team_add
        .current_dir(temp.path())
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "not in a gr2 workspace: missing .grip/workspace.toml",
        ));
}

/// Test that `gr status` fails gracefully outside a workspace
#[test]
fn test_status_outside_workspace() {
    let temp = TempDir::new().unwrap();

    let mut cmd = Command::cargo_bin("gr").unwrap();
    cmd.current_dir(temp.path())
        .arg("status")
        .assert()
        .failure()
        .stderr(predicate::str::contains("Not in a gitgrip workspace"));
}

/// Test that `gr bench --list` works
#[test]
fn test_bench_list() {
    let mut cmd = Command::cargo_bin("gr").unwrap();
    cmd.arg("bench")
        .arg("--list")
        .assert()
        .success()
        .stdout(predicate::str::contains("Available Benchmarks"));
}

/// Test that `gr bench` runs benchmarks
#[test]
fn test_bench_run() {
    let mut cmd = Command::cargo_bin("gr").unwrap();
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
    let mut cmd = Command::cargo_bin("gr").unwrap();
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

    let mut cmd = Command::cargo_bin("gr").unwrap();
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

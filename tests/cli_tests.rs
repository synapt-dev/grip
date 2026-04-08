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

#[test]
fn test_gr2_team_list_shows_registered_agents() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add_atlas = Command::cargo_bin("gr2").unwrap();
    add_atlas
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut add_opus = Command::cargo_bin("gr2").unwrap();
    add_opus
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("opus")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(&workspace_root)
        .arg("team")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("Agent workspaces"))
        .stdout(predicate::str::contains("- atlas"))
        .stdout(predicate::str::contains("- opus"));
}

#[test]
fn test_gr2_team_list_reports_empty_state() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(&workspace_root)
        .arg("team")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "No gr2 agent workspaces registered.",
        ));
}

#[test]
fn test_gr2_team_list_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(temp.path())
        .arg("team")
        .arg("list")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "not in a gr2 workspace: missing .grip/workspace.toml",
        ));
}

#[test]
fn test_gr2_team_remove_deletes_registered_agent() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add = Command::cargo_bin("gr2").unwrap();
    add.current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let agent_root = workspace_root.join("agents/atlas");
    assert!(agent_root.join("agent.toml").exists());

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(&workspace_root)
        .arg("team")
        .arg("remove")
        .arg("atlas")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Removed gr2 agent workspace 'atlas'",
        ));

    assert!(!agent_root.exists());
}

#[test]
fn test_gr2_team_remove_rejects_missing_agent() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(&workspace_root)
        .arg("team")
        .arg("remove")
        .arg("atlas")
        .assert()
        .failure()
        .stderr(predicate::str::contains("agent 'atlas' not found"));
}

#[test]
fn test_gr2_team_remove_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(temp.path())
        .arg("team")
        .arg("remove")
        .arg("atlas")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "not in a gr2 workspace: missing .grip/workspace.toml",
        ));
}

#[test]
fn test_gr2_repo_add_registers_repo() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut repo_add = Command::cargo_bin("gr2").unwrap();
    repo_add
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success()
        .stdout(predicate::str::contains(
            "Added gr2 repo 'app' -> https://github.com/synapt-dev/app.git",
        ));

    let repo_toml = std::fs::read_to_string(workspace_root.join("repos/app/repo.toml")).unwrap();
    assert!(repo_toml.contains("name = \"app\""));
    assert!(repo_toml.contains("url = \"https://github.com/synapt-dev/app.git\""));

    let registry = std::fs::read_to_string(workspace_root.join(".grip/repos.toml")).unwrap();
    assert!(registry.contains("[[repo]]"));
    assert!(registry.contains("name = \"app\""));
}

#[test]
fn test_gr2_repo_add_rejects_duplicate_repo() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut first = Command::cargo_bin("gr2").unwrap();
    first
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr2").unwrap();
    duplicate
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .failure()
        .stderr(predicate::str::contains("repo 'app' already exists"));
}

#[test]
fn test_gr2_repo_add_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut repo_add = Command::cargo_bin("gr2").unwrap();
    repo_add
        .current_dir(temp.path())
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "not in a gr2 workspace: missing .grip/workspace.toml",
        ));
}

#[test]
fn test_gr2_repo_list_shows_registered_repos() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add_app = Command::cargo_bin("gr2").unwrap();
    add_app
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut add_docs = Command::cargo_bin("gr2").unwrap();
    add_docs
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("docs")
        .arg("https://github.com/synapt-dev/docs.git")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(&workspace_root)
        .arg("repo")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("Repos"))
        .stdout(predicate::str::contains(
            "- app -> https://github.com/synapt-dev/app.git",
        ))
        .stdout(predicate::str::contains(
            "- docs -> https://github.com/synapt-dev/docs.git",
        ));
}

#[test]
fn test_gr2_repo_list_reports_empty_state() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(&workspace_root)
        .arg("repo")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("No gr2 repos registered."));
}

#[test]
fn test_gr2_repo_list_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(temp.path())
        .arg("repo")
        .arg("list")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "not in a gr2 workspace: missing .grip/workspace.toml",
        ));
}

#[test]
fn test_gr2_repo_remove_deletes_registered_repo() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add = Command::cargo_bin("gr2").unwrap();
    add.current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let repo_root = workspace_root.join("repos/app");
    assert!(repo_root.join("repo.toml").exists());

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("remove")
        .arg("app")
        .assert()
        .success()
        .stdout(predicate::str::contains("Removed gr2 repo 'app'"));

    assert!(!repo_root.exists());
    assert!(!workspace_root.join(".grip/repos.toml").exists());
}

#[test]
fn test_gr2_repo_remove_rejects_missing_repo() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("remove")
        .arg("app")
        .assert()
        .failure()
        .stderr(predicate::str::contains("repo 'app' not found"));
}

#[test]
fn test_gr2_repo_remove_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(temp.path())
        .arg("repo")
        .arg("remove")
        .arg("app")
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

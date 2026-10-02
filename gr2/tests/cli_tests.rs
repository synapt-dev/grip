//! CLI integration tests for the gr2 development binary (`gr2-dev`).
//!
//! Lives in the gr2-cli package (not root) because Cargo only sets
//! CARGO_BIN_EXE_<name> for [[bin]] targets declared by the SAME package
//! as the test binary -- these tests need CARGO_BIN_EXE_gr2-dev, which only
//! gr2-cli's own Cargo.toml declares.
//!
//! The bin is named `gr2-dev`, not `gr2`: the user-facing `gr2` command is the Python
//! console script, and a dev binary claiming the same name put two implementations
//! behind one command for anyone with `target/release` on PATH.

use assert_cmd::Command;
use predicates::prelude::*;
use tempfile::TempDir;

#[test]
fn test_gr2_help() {
    let mut cmd = Command::cargo_bin("gr2-dev").unwrap();
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
    let mut cmd = Command::cargo_bin("gr2-dev").unwrap();
    cmd.arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::contains("gr2 0.1.0"));
}

#[test]
fn test_gr2_doctor() {
    let mut cmd = Command::cargo_bin("gr2-dev").unwrap();
    cmd.arg("doctor")
        .assert()
        .success()
        .stdout(predicate::str::contains("gr2 bootstrap OK"));
}

#[test]
fn test_gr2_init_scaffolds_team_workspace() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut cmd = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut cmd = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut team_add = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut first = Command::cargo_bin("gr2-dev").unwrap();
    first
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut team_add = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add_atlas = Command::cargo_bin("gr2-dev").unwrap();
    add_atlas
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut add_opus = Command::cargo_bin("gr2-dev").unwrap();
    add_opus
        .current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("opus")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut list = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut list = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add = Command::cargo_bin("gr2-dev").unwrap();
    add.current_dir(&workspace_root)
        .arg("team")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let agent_root = workspace_root.join("agents/atlas");
    assert!(agent_root.join("agent.toml").exists());

    let mut remove = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut remove = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut remove = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut repo_add = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut first = Command::cargo_bin("gr2-dev").unwrap();
    first
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut repo_add = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add_app = Command::cargo_bin("gr2-dev").unwrap();
    add_app
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut add_docs = Command::cargo_bin("gr2-dev").unwrap();
    add_docs
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("docs")
        .arg("https://github.com/synapt-dev/docs.git")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut list = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut list = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add = Command::cargo_bin("gr2-dev").unwrap();
    add.current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let repo_root = workspace_root.join("repos/app");
    assert!(repo_root.join("repo.toml").exists());

    let mut remove = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut init = Command::cargo_bin("gr2-dev").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut remove = Command::cargo_bin("gr2-dev").unwrap();
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

    let mut remove = Command::cargo_bin("gr2-dev").unwrap();
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

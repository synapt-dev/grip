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

#[test]
fn test_gr2_unit_add_registers_unit() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success()
        .stdout(predicate::str::contains("Added gr2 unit 'atlas'"));

    let unit_toml = std::fs::read_to_string(workspace_root.join("agents/atlas/unit.toml")).unwrap();
    assert!(unit_toml.contains("name = \"atlas\""));
    assert!(unit_toml.contains("kind = \"unit\""));

    let registry = std::fs::read_to_string(workspace_root.join(".grip/units.toml")).unwrap();
    assert!(registry.contains("[[unit]]"));
    assert!(registry.contains("name = \"atlas\""));
}

#[test]
fn test_gr2_unit_add_rejects_duplicate_unit() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut first = Command::cargo_bin("gr2").unwrap();
    first
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr2").unwrap();
    duplicate
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .failure()
        .stderr(predicate::str::contains("unit 'atlas' already exists"));
}

#[test]
fn test_gr2_unit_add_rejects_invalid_name() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut invalid = Command::cargo_bin("gr2").unwrap();
    invalid
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas/dev")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "invalid unit name 'atlas/dev': use only ASCII letters, numbers, '_' or '-'",
        ));
}

#[test]
fn test_gr2_unit_list_shows_registered_units() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add_atlas = Command::cargo_bin("gr2").unwrap();
    add_atlas
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut add_opus = Command::cargo_bin("gr2").unwrap();
    add_opus
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("opus")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(&workspace_root)
        .arg("unit")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("Units"))
        .stdout(predicate::str::contains("- atlas"))
        .stdout(predicate::str::contains("- opus"));
}

#[test]
fn test_gr2_unit_list_reports_empty_state() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut list = Command::cargo_bin("gr2").unwrap();
    list.current_dir(&workspace_root)
        .arg("unit")
        .arg("list")
        .assert()
        .success()
        .stdout(predicate::str::contains("No gr2 units registered."));
}

#[test]
fn test_gr2_unit_remove_deletes_registered_unit() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut add = Command::cargo_bin("gr2").unwrap();
    add.current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let unit_root = workspace_root.join("agents/atlas");
    assert!(unit_root.join("unit.toml").exists());

    let mut remove = Command::cargo_bin("gr2").unwrap();
    remove
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("remove")
        .arg("atlas")
        .assert()
        .success()
        .stdout(predicate::str::contains("Removed gr2 unit 'atlas'"));

    assert!(!unit_root.exists());
    assert!(!workspace_root.join(".grip/units.toml").exists());
}

#[test]
fn test_gr2_unit_requires_gr2_workspace() {
    let temp = TempDir::new().unwrap();

    let mut add = Command::cargo_bin("gr2").unwrap();
    add.current_dir(temp.path())
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "not in a gr2 workspace: missing .grip/workspace.toml",
        ));
}

#[test]
fn test_gr2_spec_show_round_trips_workspace_spec() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut repo_add = Command::cargo_bin("gr2").unwrap();
    repo_add
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut show = Command::cargo_bin("gr2").unwrap();
    show.current_dir(&workspace_root)
        .arg("spec")
        .arg("show")
        .assert()
        .success()
        .stdout(predicate::str::contains("schema_version = 1"))
        .stdout(predicate::str::contains("workspace_name = \"demo\""))
        .stdout(predicate::str::contains("name = \"app\""))
        .stdout(predicate::str::contains("name = \"atlas\""));

    let spec = std::fs::read_to_string(workspace_root.join(".grip/workspace_spec.toml")).unwrap();
    assert!(spec.contains("schema_version = 1"));
    assert!(spec.contains("workspace_name = \"demo\""));
    assert!(spec.contains("path = \"repos/app\""));
    assert!(spec.contains("path = \"agents/atlas\""));

    let mut validate = Command::cargo_bin("gr2").unwrap();
    validate
        .current_dir(&workspace_root)
        .arg("spec")
        .arg("validate")
        .assert()
        .success()
        .stdout(predicate::str::contains("Workspace spec is valid"));
}

#[test]
fn test_gr2_spec_validate_detects_missing_repo_metadata() {
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
        .success();

    let mut show = Command::cargo_bin("gr2").unwrap();
    show.current_dir(&workspace_root)
        .arg("spec")
        .arg("show")
        .assert()
        .success();

    std::fs::remove_file(workspace_root.join("repos/app/repo.toml")).unwrap();

    let mut validate = Command::cargo_bin("gr2").unwrap();
    validate
        .current_dir(&workspace_root)
        .arg("spec")
        .arg("validate")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "workspace spec repo 'app' is missing repo metadata",
        ));
}

#[test]
fn test_gr2_spec_validate_detects_conflicting_unit_names() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init").arg(&workspace_root).assert().success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut show = Command::cargo_bin("gr2").unwrap();
    show.current_dir(&workspace_root)
        .arg("spec")
        .arg("show")
        .assert()
        .success();

    let spec_path = workspace_root.join(".grip/workspace_spec.toml");
    let spec = std::fs::read_to_string(&spec_path).unwrap();
    let conflicting = format!(
        "{}\n[[units]]\nname = \"atlas\"\npath = \"agents/atlas-copy\"\nrepos = []\n",
        spec.trim_end()
    );
    std::fs::write(&spec_path, conflicting).unwrap();

    let mut validate = Command::cargo_bin("gr2").unwrap();
    validate
        .current_dir(&workspace_root)
        .arg("spec")
        .arg("validate")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "workspace spec contains duplicate unit 'atlas'",
        ));
}

#[test]
fn test_gr2_plan_empty_workspace_produces_clone_all_plan() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "https://github.com/synapt-dev/app.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["app"]

[[units]]
name = "apollo"
path = "agents/apollo"
repos = []
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();
    std::fs::create_dir_all(workspace_root.join("repos/app")).unwrap();
    std::fs::write(
        workspace_root.join("repos/app/repo.toml"),
        "name = \"app\"\nurl = \"https://github.com/synapt-dev/app.git\"\n",
    )
    .unwrap();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("ExecutionPlan"))
        .stdout(predicate::str::contains("atlas\tclone"))
        .stdout(predicate::str::contains("apollo\tclone"));
}

#[test]
fn test_gr2_plan_fully_materialized_workspace_produces_noop_plan() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut repo_add = Command::cargo_bin("gr2").unwrap();
    repo_add
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut show = Command::cargo_bin("gr2").unwrap();
    show.current_dir(&workspace_root)
        .arg("spec")
        .arg("show")
        .assert()
        .success();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("no changes required"));
}

#[test]
fn test_gr2_plan_does_not_flag_repo_attachment_presence_as_drift() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut repo_add = Command::cargo_bin("gr2").unwrap();
    repo_add
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "https://github.com/synapt-dev/app.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["app"]
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("no changes required"))
        .stdout(predicate::str::contains("configure").not());
}

#[test]
fn test_gr2_plan_missing_unit_produces_single_clone_plan() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut repo_add = Command::cargo_bin("gr2").unwrap();
    repo_add
        .current_dir(&workspace_root)
        .arg("repo")
        .arg("add")
        .arg("app")
        .arg("https://github.com/synapt-dev/app.git")
        .assert()
        .success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let mut show = Command::cargo_bin("gr2").unwrap();
    show.current_dir(&workspace_root)
        .arg("spec")
        .arg("show")
        .assert()
        .success();

    std::fs::create_dir_all(workspace_root.join("agents/apollo")).unwrap();
    std::fs::write(
        workspace_root.join("agents/apollo/unit.toml"),
        "name = \"apollo\"\nkind = \"unit\"\n",
    )
    .unwrap();

    let spec_path = workspace_root.join(".grip/workspace_spec.toml");
    let spec = std::fs::read_to_string(&spec_path).unwrap();
    let with_apollo = format!(
        "{}\n[[units]]\nname = \"apollo\"\npath = \"agents/apollo\"\nrepos = []\n",
        spec.trim_end()
    );
    std::fs::write(&spec_path, with_apollo).unwrap();
    std::fs::remove_file(workspace_root.join("agents/apollo/unit.toml")).unwrap();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("apollo\tclone"))
        .stdout(predicate::str::contains("clone unit 'apollo'"));
}

#[test]
fn test_gr2_plan_rejects_invalid_unit_repo_reference() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "https://github.com/synapt-dev/app.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["missing"]
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "unit 'atlas' references missing repo 'missing'",
        ));
}

#[test]
fn test_gr2_plan_reports_when_it_generates_a_missing_workspace_spec() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let mut unit_add = Command::cargo_bin("gr2").unwrap();
    unit_add
        .current_dir(&workspace_root)
        .arg("unit")
        .arg("add")
        .arg("atlas")
        .assert()
        .success();

    let spec_path = workspace_root.join(".grip/workspace_spec.toml");
    assert!(!spec_path.exists());

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("Generated workspace spec at"))
        .stdout(predicate::str::contains("no changes required"));

    assert!(spec_path.exists());
}

#[test]
fn test_gr2_apply_materializes_missing_units_from_plan() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "https://github.com/synapt-dev/app.git"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = ["app"]
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();
    std::fs::create_dir_all(workspace_root.join("repos/app")).unwrap();
    std::fs::write(
        workspace_root.join("repos/app/repo.toml"),
        "name = \"app\"\nurl = \"https://github.com/synapt-dev/app.git\"\n",
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .success()
        .stdout(predicate::str::contains("Applied execution plan"))
        .stdout(predicate::str::contains("cloned unit 'atlas'"));

    let unit_toml = std::fs::read_to_string(workspace_root.join("agents/atlas/unit.toml")).unwrap();
    assert!(unit_toml.contains("name = \"atlas\""));
    assert!(unit_toml.contains("kind = \"unit\""));
    assert!(unit_toml.contains("repos = [\"app\"]"));
}

#[test]
fn test_gr2_apply_requires_yes_for_large_plans() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []

[[units]]
name = "apollo"
path = "agents/apollo"
repos = []

[[units]]
name = "sentinel"
path = "agents/sentinel"
repos = []

[[units]]
name = "opus"
path = "agents/opus"
repos = []
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "plan contains more than 3 operations; rerun with --yes to apply it",
        ));

    assert!(!workspace_root.join("agents/atlas/unit.toml").exists());
    assert!(!workspace_root.join("agents/apollo/unit.toml").exists());
}

#[test]
fn test_checkout_help_mentions_add_mode() {
    let mut cmd = Command::cargo_bin("gr").unwrap();
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

#[test]
fn test_checkout_add_materializes_independent_child_checkout() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_repo("lib")
        .build();

    let mut cmd = Command::cargo_bin("gr").unwrap();
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

    let mut cmd = Command::cargo_bin("gr").unwrap();
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

    let mut cmd = Command::cargo_bin("gr").unwrap();
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

    let mut cmd = Command::cargo_bin("gr").unwrap();
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

    let mut cmd = Command::cargo_bin("gr").unwrap();
    cmd.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("empty")
        .arg("--repo")
        .arg("missing")
        .assert()
        .failure()
        .stderr(predicate::str::contains(
            "no repos matched checkout filters",
        ));
}

#[test]
fn test_checkout_add_rejects_create_and_base_flags() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let mut create_cmd = Command::cargo_bin("gr").unwrap();
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

    let mut base_cmd = Command::cargo_bin("gr").unwrap();
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

    let mut cmd = Command::cargo_bin("gr").unwrap();
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

    let mut first = Command::cargo_bin("gr").unwrap();
    first
        .current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success();

    let mut duplicate = Command::cargo_bin("gr").unwrap();
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

    let mut add = Command::cargo_bin("gr").unwrap();
    add.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success();

    let mut list = Command::cargo_bin("gr").unwrap();
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

    let mut list = Command::cargo_bin("gr").unwrap();
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

    let mut list = Command::cargo_bin("gr").unwrap();
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

    let mut add = Command::cargo_bin("gr").unwrap();
    add.current_dir(&ws.workspace_root)
        .arg("checkout")
        .arg("add")
        .arg("sandbox")
        .assert()
        .success();

    assert!(checkout_root.is_dir());

    let mut remove = Command::cargo_bin("gr").unwrap();
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

    let mut remove = Command::cargo_bin("gr").unwrap();
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

    let mut remove = Command::cargo_bin("gr").unwrap();
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

// ─── gr2 apply link operations (grip#514) ──────────────────────────────

#[test]
fn test_gr2_plan_detects_missing_symlink() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    // Create a source file that the link will point to
    std::fs::write(workspace_root.join("config/shared.toml"), "key = \"value\"\n").unwrap();

    // Create the unit directory so Clone isn't planned
    std::fs::create_dir_all(workspace_root.join("agents/atlas")).unwrap();
    std::fs::write(
        workspace_root.join("agents/atlas/unit.toml"),
        "name = \"atlas\"\nkind = \"unit\"\n",
    )
    .unwrap();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []

[[units.links]]
src = "config/shared.toml"
dest = ".config/shared.toml"
kind = "symlink"
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("link"))
        .stdout(predicate::str::contains("config/shared.toml"));
}

#[test]
fn test_gr2_apply_creates_symlink() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    // Create a source file
    std::fs::write(workspace_root.join("config/shared.toml"), "key = \"value\"\n").unwrap();

    // Create the unit directory
    std::fs::create_dir_all(workspace_root.join("agents/atlas")).unwrap();
    std::fs::write(
        workspace_root.join("agents/atlas/unit.toml"),
        "name = \"atlas\"\nkind = \"unit\"\n",
    )
    .unwrap();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []

[[units.links]]
src = "config/shared.toml"
dest = ".config/shared.toml"
kind = "symlink"
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .success()
        .stdout(predicate::str::contains("symlink config/shared.toml -> agents/atlas/.config/shared.toml"));

    let link_path = workspace_root.join("agents/atlas/.config/shared.toml");
    assert!(link_path.exists(), "symlink destination should exist");
    assert!(
        link_path.symlink_metadata().unwrap().file_type().is_symlink(),
        "destination should be a symlink"
    );

    let content = std::fs::read_to_string(&link_path).unwrap();
    assert_eq!(content, "key = \"value\"\n");
}

#[test]
fn test_gr2_apply_creates_copy() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    std::fs::write(
        workspace_root.join("config/env.toml"),
        "env = \"production\"\n",
    )
    .unwrap();

    std::fs::create_dir_all(workspace_root.join("agents/apollo")).unwrap();
    std::fs::write(
        workspace_root.join("agents/apollo/unit.toml"),
        "name = \"apollo\"\nkind = \"unit\"\n",
    )
    .unwrap();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "apollo"
path = "agents/apollo"
repos = []

[[units.links]]
src = "config/env.toml"
dest = "env.toml"
kind = "copy"
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .success()
        .stdout(predicate::str::contains("copy config/env.toml -> agents/apollo/env.toml"));

    let dest_path = workspace_root.join("agents/apollo/env.toml");
    assert!(dest_path.exists(), "copy destination should exist");
    assert!(
        !dest_path.symlink_metadata().unwrap().file_type().is_symlink(),
        "copy destination should NOT be a symlink"
    );

    let content = std::fs::read_to_string(&dest_path).unwrap();
    assert_eq!(content, "env = \"production\"\n");
}

#[test]
fn test_gr2_apply_link_fails_for_missing_source() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    std::fs::create_dir_all(workspace_root.join("agents/atlas")).unwrap();
    std::fs::write(
        workspace_root.join("agents/atlas/unit.toml"),
        "name = \"atlas\"\nkind = \"unit\"\n",
    )
    .unwrap();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []

[[units.links]]
src = "nonexistent/file.toml"
dest = "file.toml"
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .failure()
        .stderr(predicate::str::contains("link source does not exist"));
}

#[test]
fn test_gr2_plan_noop_when_link_already_exists() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    std::fs::write(workspace_root.join("config/shared.toml"), "key = \"value\"\n").unwrap();

    std::fs::create_dir_all(workspace_root.join("agents/atlas/.config")).unwrap();
    std::fs::write(
        workspace_root.join("agents/atlas/unit.toml"),
        "name = \"atlas\"\nkind = \"unit\"\n",
    )
    .unwrap();
    // Pre-create the destination so the link is already satisfied
    std::fs::write(
        workspace_root.join("agents/atlas/.config/shared.toml"),
        "existing",
    )
    .unwrap();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []

[[units.links]]
src = "config/shared.toml"
dest = ".config/shared.toml"
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut plan = Command::cargo_bin("gr2").unwrap();
    plan.current_dir(&workspace_root)
        .arg("plan")
        .assert()
        .success()
        .stdout(predicate::str::contains("no changes required"));
}

#[test]
fn test_gr2_apply_records_state() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .success();

    let state_path = workspace_root.join(".grip/state/applied.toml");
    assert!(state_path.exists(), "apply should record state");

    let state = std::fs::read_to_string(&state_path).unwrap();
    assert!(state.contains("[[applied]]"), "state should contain applied entries");
    assert!(state.contains("cloned unit"), "state should record clone action");
}

#[test]
fn test_gr2_apply_mixed_clone_and_link() {
    let temp = TempDir::new().unwrap();
    let workspace_root = temp.path().join("demo-team");

    let mut init = Command::cargo_bin("gr2").unwrap();
    init.arg("init")
        .arg(&workspace_root)
        .arg("--name")
        .arg("demo")
        .assert()
        .success();

    std::fs::write(workspace_root.join("config/shared.toml"), "shared = true\n").unwrap();

    let spec = r#"
schema_version = 1
workspace_name = "demo"

[cache]
root = ".grip/cache"

[[units]]
name = "atlas"
path = "agents/atlas"
repos = []

[[units.links]]
src = "config/shared.toml"
dest = ".config/shared.toml"
kind = "symlink"
"#;
    std::fs::write(
        workspace_root.join(".grip/workspace_spec.toml"),
        spec.trim_start(),
    )
    .unwrap();

    let mut apply = Command::cargo_bin("gr2").unwrap();
    apply
        .current_dir(&workspace_root)
        .arg("apply")
        .assert()
        .success()
        .stdout(predicate::str::contains("cloned unit 'atlas'"))
        .stdout(predicate::str::contains("symlink config/shared.toml"));

    assert!(workspace_root.join("agents/atlas/unit.toml").exists());
    assert!(workspace_root.join("agents/atlas/.config/shared.toml").exists());
}

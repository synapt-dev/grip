//! E2E tests for the init command
//!
//! These tests verify the init --from-dirs workflow without requiring network access.
//! They test platform detection from repo remotes and manifest generation.

use git2::Repository;
use std::fs;
use std::process::Command;
use tempfile::TempDir;

/// Helper to create a test repo with a remote
fn create_test_repo_with_remote(dir: &std::path::Path, remote_url: &str) {
    // Initialize git repo
    let repo = Repository::init(dir).expect("Failed to init repo");

    // Create an initial commit
    let sig = git2::Signature::now("Test", "test@example.com").unwrap();
    let tree_id = {
        let mut index = repo.index().unwrap();
        // Create a file
        let file_path = dir.join("README.md");
        fs::write(&file_path, "# Test").unwrap();
        index.add_path(std::path::Path::new("README.md")).unwrap();
        index.write().unwrap();
        index.write_tree().unwrap()
    };
    let tree = repo.find_tree(tree_id).unwrap();
    repo.commit(Some("HEAD"), &sig, &sig, "Initial commit", &tree, &[])
        .unwrap();

    // Add remote
    repo.remote("origin", remote_url).unwrap();
}

/// Helper to run gr init --from-dirs in a directory
fn run_init_from_dirs(workspace_dir: &std::path::Path) -> std::process::Output {
    Command::new("cargo")
        .args([
            "run",
            "--quiet",
            "--bin",
            "gr1",
            "--",
            "init",
            "--from-dirs",
            "-p",
        ])
        .arg(workspace_dir)
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .output()
        .expect("Failed to run gr init")
}

fn workspace_manifest_path(workspace: &std::path::Path) -> std::path::PathBuf {
    workspace.join(".gitgrip/spaces/main/gripspace.yml")
}

#[test]
fn test_init_from_dirs_discovers_repos() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Create two test repos
    let repo1 = workspace.join("frontend");
    let repo2 = workspace.join("backend");
    fs::create_dir_all(&repo1).unwrap();
    fs::create_dir_all(&repo2).unwrap();

    create_test_repo_with_remote(&repo1, "git@github.com:myorg/frontend.git");
    create_test_repo_with_remote(&repo2, "git@github.com:myorg/backend.git");

    // Run init
    let output = run_init_from_dirs(workspace);

    // Check success
    assert!(output.status.success(), "init failed: {:?}", output);

    // Verify manifest created
    let manifest_path = workspace_manifest_path(workspace);
    assert!(manifest_path.exists(), "gripspace.yml not created");

    // Verify manifest content
    let manifest_content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        manifest_content.contains("frontend"),
        "frontend not in manifest"
    );
    assert!(
        manifest_content.contains("backend"),
        "backend not in manifest"
    );
    assert!(
        manifest_content.contains("github.com"),
        "github.com URL not in manifest"
    );
}

#[test]
fn test_init_from_dirs_handles_mixed_platforms() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Create repos with different platforms
    let gh_repo = workspace.join("github-app");
    let gl_repo = workspace.join("gitlab-lib");
    fs::create_dir_all(&gh_repo).unwrap();
    fs::create_dir_all(&gl_repo).unwrap();

    create_test_repo_with_remote(&gh_repo, "git@github.com:myorg/github-app.git");
    create_test_repo_with_remote(&gl_repo, "git@gitlab.com:mygroup/gitlab-lib.git");

    // Run init
    let output = run_init_from_dirs(workspace);

    // Should succeed even with mixed platforms
    assert!(
        output.status.success(),
        "init failed with mixed platforms: {:?}",
        output
    );

    // Verify manifest created
    let manifest_path = workspace_manifest_path(workspace);
    assert!(manifest_path.exists(), "gripspace.yml not created");

    // Both repos should be included
    let manifest_content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        manifest_content.contains("github-app"),
        "github-app not in manifest"
    );
    assert!(
        manifest_content.contains("gitlab-lib"),
        "gitlab-lib not in manifest"
    );
}

#[test]
fn test_init_from_dirs_handles_repos_without_remotes() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Create a repo without a remote
    let local_repo = workspace.join("local-only");
    fs::create_dir_all(&local_repo).unwrap();

    // Just init, no remote
    let repo = Repository::init(&local_repo).expect("Failed to init repo");
    let sig = git2::Signature::now("Test", "test@example.com").unwrap();
    let tree_id = {
        let mut index = repo.index().unwrap();
        let file_path = local_repo.join("README.md");
        fs::write(&file_path, "# Local").unwrap();
        index.add_path(std::path::Path::new("README.md")).unwrap();
        index.write().unwrap();
        index.write_tree().unwrap()
    };
    let tree = repo.find_tree(tree_id).unwrap();
    repo.commit(Some("HEAD"), &sig, &sig, "Initial commit", &tree, &[])
        .unwrap();

    // Run init
    let output = run_init_from_dirs(workspace);

    // Should succeed
    assert!(
        output.status.success(),
        "init failed with local-only repo: {:?}",
        output
    );

    // Verify manifest created
    let manifest_path = workspace_manifest_path(workspace);
    assert!(manifest_path.exists(), "gripspace.yml not created");

    // Local repo should be included with placeholder URL
    let manifest_content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        manifest_content.contains("local-only"),
        "local-only not in manifest"
    );
    assert!(
        manifest_content.contains("OWNER"),
        "placeholder URL not in manifest"
    );
}

#[test]
fn test_init_from_dirs_detects_azure_repos() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Create repos with Azure DevOps remotes
    let repo1 = workspace.join("azure-frontend");
    let repo2 = workspace.join("azure-backend");
    fs::create_dir_all(&repo1).unwrap();
    fs::create_dir_all(&repo2).unwrap();

    create_test_repo_with_remote(
        &repo1,
        "git@ssh.dev.azure.com:v3/myorg/myproject/azure-frontend",
    );
    create_test_repo_with_remote(
        &repo2,
        "https://dev.azure.com/myorg/myproject/_git/azure-backend",
    );

    // Run init
    let output = run_init_from_dirs(workspace);

    // Should succeed
    assert!(
        output.status.success(),
        "init failed with Azure repos: {:?}",
        output
    );

    // Verify manifest created
    let manifest_path = workspace_manifest_path(workspace);
    assert!(manifest_path.exists(), "gripspace.yml not created");

    // Both repos should be included
    let manifest_content = fs::read_to_string(&manifest_path).unwrap();
    assert!(
        manifest_content.contains("azure-frontend"),
        "azure-frontend not in manifest"
    );
    assert!(
        manifest_content.contains("azure-backend"),
        "azure-backend not in manifest"
    );
}

#[test]
fn test_init_fails_on_existing_workspace() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Create a test repo
    let repo_dir = workspace.join("app");
    fs::create_dir_all(&repo_dir).unwrap();
    create_test_repo_with_remote(&repo_dir, "git@github.com:myorg/app.git");

    // Create .gitgrip directory to simulate existing workspace
    let gitgrip_dir = workspace.join(".gitgrip");
    fs::create_dir_all(&gitgrip_dir).unwrap();

    // Run init - should fail
    let output = run_init_from_dirs(workspace);

    assert!(
        !output.status.success(),
        "init should fail on existing workspace"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("already exists") || stderr.contains("reinitialize"),
        "should mention existing workspace: {}",
        stderr
    );
}

#[test]
fn test_init_fails_on_empty_directory() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Empty directory - no repos

    // Run init - should fail
    let output = run_init_from_dirs(workspace);

    assert!(
        !output.status.success(),
        "init should fail on empty directory"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("No git repositories found"),
        "should mention no repos found: {}",
        stderr
    );
}

#[test]
fn test_init_skips_hidden_directories() {
    let temp = TempDir::new().unwrap();
    let workspace = temp.path();

    // Create a hidden directory with a repo
    let hidden_repo = workspace.join(".hidden-repo");
    fs::create_dir_all(&hidden_repo).unwrap();
    create_test_repo_with_remote(&hidden_repo, "git@github.com:myorg/hidden.git");

    // Create a visible repo
    let visible_repo = workspace.join("visible-app");
    fs::create_dir_all(&visible_repo).unwrap();
    create_test_repo_with_remote(&visible_repo, "git@github.com:myorg/visible-app.git");

    // Run init
    let output = run_init_from_dirs(workspace);

    assert!(output.status.success(), "init failed: {:?}", output);

    // Verify manifest only includes visible repo
    let manifest_path = workspace_manifest_path(workspace);
    let manifest_content = fs::read_to_string(&manifest_path).unwrap();

    assert!(
        manifest_content.contains("visible-app"),
        "visible-app should be in manifest"
    );
    assert!(
        !manifest_content.contains("hidden-repo"),
        "hidden-repo should not be in manifest"
    );
}

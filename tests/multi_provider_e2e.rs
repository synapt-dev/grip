//! Multi-provider E2E tests
//!
//! These tests verify gitgrip operations across GitHub, GitLab, and Azure DevOps.
//! They use real test repositories and require authentication.
//!
//! Test repos (consistent naming across platforms):
//! - GitHub: laynepenney/gitgrip-test-1, gitgrip-test-2, gitgrip-test-manifest
//! - GitLab: laynepenney/gitgrip-test-1, gitgrip-test-2, gitgrip-test-manifest
//! - Azure DevOps: laynepenney/gitgrip/gitgrip-test-1, gitgrip-test-2, gitgrip-test-manifest
//!
//! Run with: cargo test --features integration-tests --test multi_provider_e2e -- --ignored

use std::fs;
use std::path::Path;
use std::process::Command;
use tempfile::TempDir;

/// Test repo URLs for each platform
mod test_repos {
    pub mod github {
        pub const REPO1: &str = "git@github.com:laynepenney/gitgrip-test-1.git";
        pub const REPO2: &str = "git@github.com:laynepenney/gitgrip-test-2.git";
        pub const MANIFEST: &str = "git@github.com:laynepenney/gitgrip-test-manifest.git";
    }

    pub mod gitlab {
        pub const REPO1: &str = "git@gitlab.com:laynepenney/gitgrip-test-1.git";
        pub const REPO2: &str = "git@gitlab.com:laynepenney/gitgrip-test-2.git";
        pub const MANIFEST: &str = "git@gitlab.com:laynepenney/gitgrip-test-manifest.git";
    }

    pub mod azure {
        pub const REPO1: &str = "git@ssh.dev.azure.com:v3/laynepenney/gitgrip/gitgrip-test-1";
        pub const REPO2: &str = "git@ssh.dev.azure.com:v3/laynepenney/gitgrip/gitgrip-test-2";
        pub const MANIFEST: &str =
            "git@ssh.dev.azure.com:v3/laynepenney/gitgrip/gitgrip-test-manifest";
    }
}

fn random_suffix() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let duration = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    format!("{}", duration.as_millis() % 1_000_000)
}

/// Get path to the gr binary (builds it if needed)
fn gr_binary() -> std::path::PathBuf {
    // Build the binary first
    let status = Command::new("cargo")
        .args(["build", "--quiet", "--bin", "gr"])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .status()
        .expect("Failed to build gr");
    assert!(status.success(), "Failed to build gr binary");

    // Return path to the binary
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/debug/gr")
}

/// Run gr command in the given workspace directory
/// For init, uses -p flag. For other commands, runs from workspace dir.
fn run_gr(args: &[&str], workspace: &Path) -> std::process::Output {
    let binary = gr_binary();

    // Check if this is an init command (needs -p flag)
    if args.first() == Some(&"init") {
        Command::new(&binary)
            .args(args)
            .current_dir(env!("CARGO_MANIFEST_DIR"))
            .output()
            .expect("Failed to run gr")
    } else {
        // Other commands - run from workspace directory
        Command::new(&binary)
            .args(args)
            .current_dir(workspace)
            .output()
            .expect("Failed to run gr")
    }
}

/// Run gr command in a directory (alias for run_gr for clarity)
fn run_gr_in_dir(args: &[&str], workspace: &Path) -> std::process::Output {
    run_gr(args, workspace)
}

/// Clone a repo and return the path
fn clone_repo(url: &str, name: &str, parent: &Path) -> std::path::PathBuf {
    let repo_path = parent.join(name);
    let output = Command::new("git")
        .args(["clone", url, name])
        .current_dir(parent)
        .output()
        .expect("Failed to clone");

    if !output.status.success() {
        panic!(
            "Failed to clone {}: {}",
            url,
            String::from_utf8_lossy(&output.stderr)
        );
    }

    repo_path
}

/// Create a test file with unique content
fn create_test_file(repo_path: &Path, filename: &str) -> String {
    let content = format!("Test content: {}\n", random_suffix());
    let file_path = repo_path.join(filename);
    fs::write(&file_path, &content).expect("Failed to write test file");
    content
}

/// Git operations in a repo
fn git_in_repo(args: &[&str], repo_path: &Path) -> std::process::Output {
    Command::new("git")
        .args(args)
        .current_dir(repo_path)
        .output()
        .expect("Failed to run git")
}

// ==================== GitHub Tests ====================

#[cfg(feature = "integration-tests")]
mod github_tests {
    use super::*;

    #[test]
    #[ignore = "Requires GitHub SSH access"]
    fn test_github_init_from_cloned_repos() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        // Clone test repos
        clone_repo(test_repos::github::REPO1, "repo1", workspace);
        clone_repo(test_repos::github::REPO2, "repo2", workspace);

        // Run gr init --from-dirs
        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );

        assert!(
            output.status.success(),
            "init failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Verify manifest
        let manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(workspace)
                .expect("manifest not found");
        let manifest = fs::read_to_string(&manifest_path).unwrap();
        assert!(manifest.contains("repo1"), "repo1 not in manifest");
        assert!(manifest.contains("repo2"), "repo2 not in manifest");
        assert!(
            manifest.contains("github.com"),
            "github.com not in manifest"
        );
    }

    #[test]
    #[ignore = "Requires GitHub SSH access"]
    fn test_github_init_from_manifest_url() {
        let temp = TempDir::new().unwrap();
        // Use a subdirectory that doesn't exist yet
        let workspace = temp.path().join("workspace");

        // Initialize from manifest URL
        let output = run_gr(
            &[
                "init",
                test_repos::github::MANIFEST,
                "-p",
                workspace.to_str().unwrap(),
            ],
            &workspace,
        );

        assert!(
            output.status.success(),
            "init from manifest failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Sync to clone the repos defined in the manifest
        let output = run_gr(&["sync"], &workspace);
        assert!(
            output.status.success(),
            "sync failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Verify repos were cloned
        assert!(workspace.join("repo1").exists(), "repo1 not cloned");
        assert!(workspace.join("repo2").exists(), "repo2 not cloned");
    }

    #[test]
    #[ignore = "Requires GitHub SSH and API access"]
    fn test_github_full_pr_workflow() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/e2e-{}", random_suffix());

        // Clone test repos
        clone_repo(test_repos::github::REPO1, "repo1", workspace);
        clone_repo(test_repos::github::REPO2, "repo2", workspace);

        // Initialize workspace
        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success(), "init failed");

        // Create branch
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(
            output.status.success(),
            "branch failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Make changes
        create_test_file(&workspace.join("repo1"), "test-file.txt");
        create_test_file(&workspace.join("repo2"), "test-file.txt");

        // Stage and commit
        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(output.status.success(), "add failed");

        let output = run_gr_in_dir(&["commit", "-m", "test: e2e test commit"], workspace);
        assert!(
            output.status.success(),
            "commit failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Push
        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(
            output.status.success(),
            "push failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Create PR
        let output = run_gr_in_dir(&["pr", "create", "-t", "test: E2E test PR"], workspace);
        assert!(
            output.status.success(),
            "pr create failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Check PR status
        let output = run_gr_in_dir(&["pr", "status"], workspace);
        assert!(
            output.status.success(),
            "pr status failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Cleanup: close PRs and delete branch
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo1"),
        );
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo2"),
        );
    }

    #[test]
    #[ignore = "Requires GitHub SSH access"]
    fn test_github_status_shows_all_repos() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::github::REPO1, "repo1", workspace);
        clone_repo(test_repos::github::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        let output = run_gr_in_dir(&["status"], workspace);
        assert!(
            output.status.success(),
            "status failed: stdout={}, stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );

        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            stdout.contains("repo1") || stdout.contains("gitgrip-test-1"),
            "repo1/gitgrip-test-1 not in status output: {}",
            stdout
        );
        assert!(
            stdout.contains("repo2") || stdout.contains("gitgrip-test-2"),
            "repo2/gitgrip-test-2 not in status output: {}",
            stdout
        );
    }
}

// ==================== GitLab Tests ====================

#[cfg(feature = "integration-tests")]
mod gitlab_tests {
    use super::*;

    #[test]
    #[ignore = "Requires GitLab SSH access"]
    fn test_gitlab_init_from_cloned_repos() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::gitlab::REPO1, "repo1", workspace);
        clone_repo(test_repos::gitlab::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );

        assert!(
            output.status.success(),
            "init failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(workspace)
                .expect("manifest not found");
        let manifest = fs::read_to_string(&manifest_path).unwrap();
        assert!(manifest.contains("repo1"));
        assert!(manifest.contains("repo2"));
        assert!(manifest.contains("gitlab.com"));
    }

    #[test]
    #[ignore = "Requires GitLab SSH access"]
    fn test_gitlab_init_from_manifest_url() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().join("workspace");

        let output = run_gr(
            &[
                "init",
                test_repos::gitlab::MANIFEST,
                "-p",
                workspace.to_str().unwrap(),
            ],
            &workspace,
        );

        assert!(
            output.status.success(),
            "init from manifest failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Sync to clone the repos
        let output = run_gr(&["sync"], &workspace);
        assert!(
            output.status.success(),
            "sync failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        assert!(workspace.join("repo1").exists(), "repo1 not cloned");
        assert!(workspace.join("repo2").exists(), "repo2 not cloned");
    }

    #[test]
    #[ignore = "Requires GitLab SSH and API access"]
    fn test_gitlab_branch_and_push() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/e2e-{}", random_suffix());

        clone_repo(test_repos::gitlab::REPO1, "repo1", workspace);
        clone_repo(test_repos::gitlab::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Create branch
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(
            output.status.success(),
            "branch failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Make changes and commit
        create_test_file(&workspace.join("repo1"), "gitlab-test.txt");

        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(output.status.success());

        let output = run_gr_in_dir(&["commit", "-m", "test: gitlab e2e"], workspace);
        assert!(output.status.success());

        // Push
        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(
            output.status.success(),
            "push failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Cleanup
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo1"),
        );
    }

    #[test]
    #[ignore = "Requires GitLab SSH and API access"]
    fn test_gitlab_full_pr_workflow() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/e2e-{}", random_suffix());

        // Clone test repos
        clone_repo(test_repos::gitlab::REPO1, "repo1", workspace);
        clone_repo(test_repos::gitlab::REPO2, "repo2", workspace);

        // Initialize workspace
        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(
            output.status.success(),
            "init failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        println!("Init output: {}", String::from_utf8_lossy(&output.stdout));

        // Read and print the manifest
        let manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(workspace)
                .expect("manifest not found");
        if manifest_path.exists() {
            let manifest_content = fs::read_to_string(&manifest_path).unwrap();
            println!("Manifest content:\n{}", manifest_content);
        } else {
            println!(
                "WARNING: Manifest file does not exist at {:?}",
                manifest_path
            );
        }

        // Check initial status
        let output = run_gr_in_dir(&["status"], workspace);
        println!(
            "Initial status: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        // Create branch
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(
            output.status.success(),
            "branch failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        println!(
            "Branch created: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        // Make changes in both repos
        create_test_file(&workspace.join("repo1"), "test-file.txt");
        create_test_file(&workspace.join("repo2"), "test-file.txt");

        // Check status after changes
        let output = run_gr_in_dir(&["status"], workspace);
        println!(
            "Status after changes: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        // Stage and commit
        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(
            output.status.success(),
            "add failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        println!("Add output: {}", String::from_utf8_lossy(&output.stdout));

        let output = run_gr_in_dir(&["commit", "-m", "test: gitlab e2e PR test"], workspace);
        assert!(
            output.status.success(),
            "commit failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        println!("Commit output: {}", String::from_utf8_lossy(&output.stdout));

        // Check status after commit
        let output = run_gr_in_dir(&["status"], workspace);
        println!(
            "Status after commit: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        // Push
        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(
            output.status.success(),
            "push failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        println!("Push output: {}", String::from_utf8_lossy(&output.stdout));

        // Check status after push
        let output = run_gr_in_dir(&["status"], workspace);
        println!(
            "Status after push: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        // Debug: Check git references in repo1
        let output = git_in_repo(&["branch", "-a"], &workspace.join("repo1"));
        println!(
            "Git branches in repo1: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        let output = git_in_repo(&["log", "--oneline", "-5"], &workspace.join("repo1"));
        println!(
            "Git log in repo1: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        let output = git_in_repo(
            &["log", "--oneline", "-5", "origin/main"],
            &workspace.join("repo1"),
        );
        println!(
            "Git log origin/main in repo1: {}",
            String::from_utf8_lossy(&output.stdout)
        );

        // Create PR (merge request on GitLab)
        let output = run_gr_in_dir(
            &["pr", "create", "-t", "test: GitLab E2E test MR"],
            workspace,
        );

        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        println!("PR create stdout: {}", stdout);
        println!("PR create stderr: {}", stderr);

        assert!(
            output.status.success(),
            "pr create failed: stdout={}, stderr={}",
            stdout,
            stderr
        );

        // Verify PRs were created (check for actual creation, not just "PR" text)
        assert!(
            stdout.contains("Created") && stdout.contains("#"),
            "Expected PR creation with number in output, got: {}",
            stdout
        );

        // Check PR status
        let output = run_gr_in_dir(&["pr", "status"], workspace);
        assert!(
            output.status.success(),
            "pr status failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let stdout = String::from_utf8_lossy(&output.stdout);
        println!("PR status output: {}", stdout);

        // Cleanup: delete remote branches (this will auto-close the MRs on GitLab)
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo1"),
        );
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo2"),
        );
    }
}

// ==================== Azure DevOps Tests ====================

#[cfg(feature = "integration-tests")]
mod azure_tests {
    use super::*;

    #[test]
    #[ignore = "Requires Azure DevOps SSH access"]
    fn test_azure_init_from_cloned_repos() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::azure::REPO1, "repo1", workspace);
        clone_repo(test_repos::azure::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );

        assert!(
            output.status.success(),
            "init failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(workspace)
                .expect("manifest not found");
        let manifest = fs::read_to_string(&manifest_path).unwrap();
        assert!(manifest.contains("repo1"));
        assert!(manifest.contains("repo2"));
        assert!(
            manifest.contains("dev.azure.com") || manifest.contains("ssh.dev.azure.com"),
            "Azure DevOps URL not in manifest"
        );
    }

    #[test]
    #[ignore = "Requires Azure DevOps SSH access"]
    fn test_azure_init_from_manifest_url() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path().join("workspace");

        let output = run_gr(
            &[
                "init",
                test_repos::azure::MANIFEST,
                "-p",
                workspace.to_str().unwrap(),
            ],
            &workspace,
        );

        assert!(
            output.status.success(),
            "init from manifest failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Sync to clone the repos
        let output = run_gr(&["sync"], &workspace);
        assert!(
            output.status.success(),
            "sync failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        assert!(workspace.join("repo1").exists(), "repo1 not cloned");
        assert!(workspace.join("repo2").exists(), "repo2 not cloned");
    }

    #[test]
    #[ignore = "Requires Azure DevOps SSH access"]
    fn test_azure_branch_and_push() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/e2e-{}", random_suffix());

        clone_repo(test_repos::azure::REPO1, "repo1", workspace);
        clone_repo(test_repos::azure::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Create branch
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(
            output.status.success(),
            "branch failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Make changes
        create_test_file(&workspace.join("repo1"), "azure-test.txt");

        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(output.status.success());

        let output = run_gr_in_dir(&["commit", "-m", "test: azure e2e"], workspace);
        assert!(output.status.success());

        // Push
        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(
            output.status.success(),
            "push failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Cleanup
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo1"),
        );
    }

    #[test]
    #[ignore = "Requires Azure DevOps SSH and API access"]
    fn test_azure_full_pr_workflow() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/e2e-{}", random_suffix());

        // Clone test repos
        clone_repo(test_repos::azure::REPO1, "repo1", workspace);
        clone_repo(test_repos::azure::REPO2, "repo2", workspace);

        // Initialize workspace
        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success(), "init failed");

        // Create branch
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(
            output.status.success(),
            "branch failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Make changes in both repos
        create_test_file(&workspace.join("repo1"), "test-file.txt");
        create_test_file(&workspace.join("repo2"), "test-file.txt");

        // Stage and commit
        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(output.status.success(), "add failed");

        let output = run_gr_in_dir(&["commit", "-m", "test: azure e2e PR test"], workspace);
        assert!(
            output.status.success(),
            "commit failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Push
        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(
            output.status.success(),
            "push failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Create PR
        let output = run_gr_in_dir(
            &["pr", "create", "-t", "test: Azure DevOps E2E test PR"],
            workspace,
        );

        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        println!("PR create stdout: {}", stdout);
        println!("PR create stderr: {}", stderr);

        assert!(
            output.status.success(),
            "pr create failed: stdout={}, stderr={}",
            stdout,
            stderr
        );

        // Verify PRs were created (check for actual creation, not just "PR" text)
        assert!(
            stdout.contains("Created") && stdout.contains("#"),
            "Expected PR creation with number in output, got: {}",
            stdout
        );

        // Check PR status
        let output = run_gr_in_dir(&["pr", "status"], workspace);
        assert!(
            output.status.success(),
            "pr status failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let stdout = String::from_utf8_lossy(&output.stdout);
        println!("PR status output: {}", stdout);

        // Cleanup: delete remote branches
        // Note: On Azure DevOps, deleting the source branch will abandon the PR
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo1"),
        );
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("repo2"),
        );
    }
}

// ==================== Mixed Platform Tests ====================

#[cfg(feature = "integration-tests")]
mod mixed_platform_tests {
    use super::*;

    #[test]
    #[ignore = "Requires SSH access to GitHub, GitLab, and Azure DevOps"]
    fn test_mixed_platform_init() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        // Clone one repo from each platform
        clone_repo(test_repos::github::REPO1, "github-repo", workspace);
        clone_repo(test_repos::gitlab::REPO1, "gitlab-repo", workspace);
        clone_repo(test_repos::azure::REPO1, "azure-repo", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );

        assert!(
            output.status.success(),
            "init failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let manifest_path =
            gitgrip::core::manifest_paths::resolve_gripspace_manifest_path(workspace)
                .expect("manifest not found");
        let manifest = fs::read_to_string(&manifest_path).unwrap();

        // All repos should be in manifest
        assert!(
            manifest.contains("github-repo"),
            "github-repo not in manifest"
        );
        assert!(
            manifest.contains("gitlab-repo"),
            "gitlab-repo not in manifest"
        );
        assert!(
            manifest.contains("azure-repo"),
            "azure-repo not in manifest"
        );

        // All platforms should be detected
        assert!(manifest.contains("github.com"), "github.com not detected");
        assert!(manifest.contains("gitlab.com"), "gitlab.com not detected");
        assert!(
            manifest.contains("dev.azure.com") || manifest.contains("ssh.dev.azure.com"),
            "Azure DevOps not detected"
        );
    }

    #[test]
    #[ignore = "Requires SSH access to GitHub, GitLab, and Azure DevOps"]
    fn test_mixed_platform_status() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::github::REPO1, "github-repo", workspace);
        clone_repo(test_repos::gitlab::REPO1, "gitlab-repo", workspace);
        clone_repo(test_repos::azure::REPO1, "azure-repo", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        let output = run_gr_in_dir(&["status"], workspace);
        assert!(output.status.success());

        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("github-repo"));
        assert!(stdout.contains("gitlab-repo"));
        assert!(stdout.contains("azure-repo"));
    }

    #[test]
    #[ignore = "Requires SSH access to GitHub, GitLab, and Azure DevOps"]
    fn test_mixed_platform_branch_across_all() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/mixed-{}", random_suffix());

        clone_repo(test_repos::github::REPO1, "github-repo", workspace);
        clone_repo(test_repos::gitlab::REPO1, "gitlab-repo", workspace);
        clone_repo(test_repos::azure::REPO1, "azure-repo", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Create branch across all platforms
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(
            output.status.success(),
            "branch failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Verify branch exists in each repo
        for repo_name in &["github-repo", "gitlab-repo", "azure-repo"] {
            let output = git_in_repo(
                &["branch", "--list", &branch_name],
                &workspace.join(repo_name),
            );
            let stdout = String::from_utf8_lossy(&output.stdout);
            assert!(
                stdout.contains(&branch_name),
                "Branch not created in {}",
                repo_name
            );
        }

        // Make changes in each repo
        create_test_file(&workspace.join("github-repo"), "mixed-test.txt");
        create_test_file(&workspace.join("gitlab-repo"), "mixed-test.txt");
        create_test_file(&workspace.join("azure-repo"), "mixed-test.txt");

        // Stage, commit, and push
        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(output.status.success());

        let output = run_gr_in_dir(&["commit", "-m", "test: mixed platform e2e"], workspace);
        assert!(output.status.success());

        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(
            output.status.success(),
            "push failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        // Cleanup: delete remote branches
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("github-repo"),
        );
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("gitlab-repo"),
        );
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("azure-repo"),
        );
    }

    #[test]
    #[ignore = "Requires SSH access to GitHub and GitLab"]
    fn test_mixed_github_gitlab_pr_workflow() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();
        let branch_name = format!("test/pr-mixed-{}", random_suffix());

        // Use GitHub and GitLab (Azure DevOps PRs work differently)
        clone_repo(test_repos::github::REPO1, "github-app", workspace);
        clone_repo(test_repos::gitlab::REPO1, "gitlab-app", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Create branch
        let output = run_gr_in_dir(&["branch", &branch_name], workspace);
        assert!(output.status.success());

        // Make changes
        create_test_file(&workspace.join("github-app"), "pr-test.txt");
        create_test_file(&workspace.join("gitlab-app"), "pr-test.txt");

        // Commit and push
        let output = run_gr_in_dir(&["add", "."], workspace);
        assert!(output.status.success());

        let output = run_gr_in_dir(&["commit", "-m", "test: mixed PR test"], workspace);
        assert!(output.status.success());

        let output = run_gr_in_dir(&["push", "-u"], workspace);
        assert!(output.status.success());

        // Create PRs (should create on both platforms)
        let output = run_gr_in_dir(
            &["pr", "create", "-t", "test: Mixed platform PR"],
            workspace,
        );

        // Note: This might partially succeed if one platform fails
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        println!("PR create stdout: {}", stdout);
        println!("PR create stderr: {}", stderr);

        // Cleanup
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("github-app"),
        );
        git_in_repo(
            &["push", "origin", "--delete", &branch_name],
            &workspace.join("gitlab-app"),
        );
    }
}

// ==================== Sync Tests ====================

#[cfg(feature = "integration-tests")]
mod sync_tests {
    use super::*;

    #[test]
    #[ignore = "Requires GitHub SSH access"]
    fn test_sync_pulls_latest_changes() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::github::REPO1, "repo1", workspace);
        clone_repo(test_repos::github::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Run sync
        let output = run_gr_in_dir(&["sync"], workspace);
        assert!(
            output.status.success(),
            "sync failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

// ==================== Forall Tests ====================

#[cfg(feature = "integration-tests")]
mod forall_tests {
    use super::*;

    #[test]
    #[ignore = "Requires GitHub SSH access"]
    fn test_forall_runs_command_in_all_repos() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::github::REPO1, "repo1", workspace);
        clone_repo(test_repos::github::REPO2, "repo2", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Run forall with a simple command (--all to include repos without changes)
        let output = run_gr_in_dir(&["forall", "--all", "-c", "pwd"], workspace);
        assert!(
            output.status.success(),
            "forall failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );

        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("repo1"), "repo1 not in forall output");
        assert!(stdout.contains("repo2"), "repo2 not in forall output");
    }

    #[test]
    #[ignore = "Requires SSH access to all platforms"]
    fn test_forall_works_across_platforms() {
        let temp = TempDir::new().unwrap();
        let workspace = temp.path();

        clone_repo(test_repos::github::REPO1, "github-app", workspace);
        clone_repo(test_repos::gitlab::REPO1, "gitlab-app", workspace);
        clone_repo(test_repos::azure::REPO1, "azure-app", workspace);

        let output = run_gr(
            &["init", "--from-dirs", "-p", workspace.to_str().unwrap()],
            workspace,
        );
        assert!(output.status.success());

        // Run forall to show remote URLs (--all to include repos without changes)
        let output = run_gr_in_dir(
            &["forall", "--all", "-c", "git remote get-url origin"],
            workspace,
        );
        assert!(output.status.success());

        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("github.com"));
        assert!(stdout.contains("gitlab.com"));
        assert!(stdout.contains("dev.azure.com") || stdout.contains("ssh.dev.azure.com"));
    }
}

// Non-feature-gated test to verify compilation
#[test]
fn test_multi_provider_e2e_module_compiles() {
    assert!(true);
}

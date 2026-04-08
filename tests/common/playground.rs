//! Disposable playground harness for binary-level CLI flows.
//!
//! This is the proving-ground layer for future team-workspace work. It starts
//! from plain local git repos, initializes a workspace via `gr init --from-dirs`,
//! and then drives real `gr` commands against that disposable workspace.

use std::path::{Path, PathBuf};
use tempfile::TempDir;

use super::git_helpers;

pub struct PlaygroundHarness {
    pub _temp: TempDir,
    pub workspace_root: PathBuf,
    pub repo_names: Vec<String>,
}

impl PlaygroundHarness {
    pub fn new(repo_names: &[&str]) -> Self {
        let temp = TempDir::new().expect("failed to create temp dir");
        let workspace_root = temp.path().join("playground");
        let remotes_dir = temp.path().join("remotes");
        std::fs::create_dir_all(&workspace_root).expect("failed to create workspace root");
        std::fs::create_dir_all(&remotes_dir).expect("failed to create remotes dir");

        for repo_name in repo_names {
            let bare_path = remotes_dir.join(format!("{}.git", repo_name));
            let staging = temp.path().join(format!("staging-{}", repo_name));
            let repo_path = workspace_root.join(repo_name);

            git_helpers::init_bare_repo(&bare_path);
            git_helpers::init_repo(&staging);
            git_helpers::commit_file(
                &staging,
                "README.md",
                &format!("# {}\n", repo_name),
                "Initial commit",
            );

            let remote_url = format!("file://{}", bare_path.display());
            git_helpers::add_remote(&staging, "origin", &remote_url);
            git_helpers::push_upstream(&staging, "origin", "main");
            git_helpers::clone_repo(&remote_url, &repo_path);
        }

        Self {
            _temp: temp,
            workspace_root,
            repo_names: repo_names.iter().map(|name| (*name).to_string()).collect(),
        }
    }

    pub fn repo_path(&self, name: &str) -> PathBuf {
        self.workspace_root.join(name)
    }

    pub fn init_from_dirs(&self) {
        self.run_from_manifest_dir([
            "init",
            "--from-dirs",
            "-p",
            self.workspace_root
                .to_str()
                .expect("workspace path should be utf-8"),
        ]);
    }

    pub fn run_in_workspace<I, S>(&self, args: I)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.run(args, &self.workspace_root);
    }

    pub fn run_from_manifest_dir<I, S>(&self, args: I)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.run(args, Path::new(env!("CARGO_MANIFEST_DIR")));
    }

    fn run<I, S>(&self, args: I, current_dir: &Path)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let args_vec: Vec<String> = args.into_iter().map(|s| s.as_ref().to_string()).collect();
        let mut cmd = std::process::Command::new(assert_cmd::cargo::cargo_bin!("gr"));
        let output = cmd
            .current_dir(current_dir)
            .args(args_vec.iter().map(String::as_str))
            .output()
            .expect("failed to run gr binary");
        assert!(
            output.status.success(),
            "gr {:?} failed in {}:\nstdout:\n{}\nstderr:\n{}",
            args_vec,
            current_dir.display(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

//! `gr link --apply` must not certify stale gripspace-derived output.
//!
//! The command composes from clones under `.gitgrip/spaces`. A valid local
//! file proves only that composition can run. It does not prove that the clone
//! still reflects its upstream. This test advances the upstream after a known
//! current baseline and requires the manual apply path to refuse before it
//! rewrites the destination from stale bytes.

use assert_cmd::Command;
use std::path::{Path, PathBuf};
use std::process::Command as StdCommand;
use tempfile::TempDir;

fn git(dir: &Path, args: &[&str]) -> String {
    let output = StdCommand::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {:?} failed in {}: {}",
        args,
        dir.display(),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn repo(root: &Path, name: &str, files: &[(&str, &str)]) -> PathBuf {
    let dir = root.join(name);
    std::fs::create_dir_all(&dir).unwrap();
    git(&dir, &["init", "-q", "-b", "main"]);
    git(&dir, &["config", "user.email", "test@example.com"]);
    git(&dir, &["config", "user.name", "test"]);
    // Byte-exact checkout across gr's clone (Windows runner autocrlf=true would
    // otherwise CRLF-convert committed `\n` content). See the fuller note on
    // link_apply_detached_freshness::repo. `.gitattributes` travels with clones.
    git(&dir, &["config", "core.autocrlf", "false"]);
    std::fs::write(dir.join(".gitattributes"), "* -text\n").unwrap();
    for (path, body) in files {
        let full = dir.join(path);
        if let Some(parent) = full.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(full, body).unwrap();
    }
    git(&dir, &["add", "-A"]);
    git(&dir, &["commit", "-qm", "initial"]);
    dir
}

fn advance(repo: &Path, body: &str) -> String {
    std::fs::write(repo.join("SECTION.md"), body).unwrap();
    git(repo, &["add", "SECTION.md"]);
    git(repo, &["commit", "-qm", "advance source"]);
    git(repo, &["rev-parse", "HEAD"])
}

#[test]
fn link_apply_refuses_a_gripspace_source_that_is_behind_upstream() {
    let fixture = TempDir::new().unwrap();
    let root = fixture.path();

    let source = repo(
        root,
        "source-space",
        &[
            ("SECTION.md", "version-one-content\n"),
            ("gripspace.yml", "version: 2\nrepos: {}\n"),
        ],
    );
    let dummy = repo(root, "dummy-repo", &[("README.md", "dummy\n")]);
    let manifest = repo(
        root,
        "workspace-manifest",
        &[(
            "gripspace.yml",
            &format!(
                r#"version: 2
gripspaces:
  - url: '{}'
    rev: main
manifest:
  url: '{}'
  revision: main
  composefile:
    - dest: OUT.md
      parts:
        - gripspace: source-space
          src: SECTION.md
repos:
  dummy-repo:
    url: '{}'
    path: ./dummy-repo
    revision: main
"#,
                source.display(),
                root.join("workspace-manifest").display(),
                dummy.display(),
            ),
        )],
    );

    let workspace = root.join("workspace");
    let init = Command::cargo_bin("gr1")
        .unwrap()
        .args([
            "init",
            manifest.to_str().unwrap(),
            "--path",
            workspace.to_str().unwrap(),
            "--no-interactive",
        ])
        .output()
        .unwrap();
    assert!(
        init.status.success(),
        "init precondition failed:\nstdout={}\nstderr={}",
        String::from_utf8_lossy(&init.stdout),
        String::from_utf8_lossy(&init.stderr)
    );

    // Establish a known-current composition through the same sync path the
    // issue uses before advancing the source. `gr init` currently treats a
    // link-application failure as a warning, so its zero exit alone is not a
    // sufficient fixture precondition.
    let baseline_sync = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert!(
        baseline_sync.status.success(),
        "baseline sync precondition failed:\nmanifest={}\nstdout={}\nstderr={}",
        std::fs::read_to_string(workspace.join(".gitgrip/spaces/main/gripspace.yml"))
            .unwrap_or_else(|error| format!("<unreadable: {error}>")),
        String::from_utf8_lossy(&baseline_sync.stdout),
        String::from_utf8_lossy(&baseline_sync.stderr)
    );
    assert_eq!(
        std::fs::read_to_string(workspace.join("OUT.md")).unwrap_or_else(|error| {
            panic!(
                "baseline composition did not produce OUT.md: {error}\nstdout={}\nstderr={}",
                String::from_utf8_lossy(&baseline_sync.stdout),
                String::from_utf8_lossy(&baseline_sync.stderr)
            )
        }),
        "version-one-content\n"
    );

    advance(&source, "version-two-content\n");
    let sync = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert!(
        sync.status.success(),
        "sync precondition failed:\nstdout={}\nstderr={}",
        String::from_utf8_lossy(&sync.stdout),
        String::from_utf8_lossy(&sync.stderr)
    );
    assert_eq!(
        std::fs::read_to_string(workspace.join("OUT.md")).unwrap(),
        "version-two-content\n"
    );

    let upstream_head = advance(&source, "version-three-content\n");
    let local_head = git(
        &workspace.join(".gitgrip/spaces/source-space"),
        &["rev-parse", "HEAD"],
    );
    assert_ne!(
        local_head, upstream_head,
        "fixture must be stale before apply"
    );

    let apply = Command::cargo_bin("gr1")
        .unwrap()
        .args(["link", "--apply"])
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert_eq!(
        apply.status.code(),
        Some(2),
        "a stale source must be a deliberate refusal, not success:\nstdout={}\nstderr={}",
        String::from_utf8_lossy(&apply.stdout),
        String::from_utf8_lossy(&apply.stderr)
    );
    let diagnostic = format!(
        "{}{}",
        String::from_utf8_lossy(&apply.stdout),
        String::from_utf8_lossy(&apply.stderr)
    );
    assert!(
        diagnostic.contains("source-space"),
        "diagnostic must name the stale source"
    );
    assert!(
        diagnostic.contains("gr sync"),
        "diagnostic must state the recovery path"
    );
    assert_eq!(
        std::fs::read_to_string(workspace.join("OUT.md")).unwrap(),
        "version-two-content\n",
        "refusal must happen before stale bytes rewrite the destination"
    );

    // Positive control: after the named recovery path, the same command runs
    // and the destination reflects the advanced source.
    let sync = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert!(sync.status.success());
    let apply = Command::cargo_bin("gr1")
        .unwrap()
        .args(["link", "--apply"])
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert!(apply.status.success());
    assert_eq!(
        std::fs::read_to_string(workspace.join("OUT.md")).unwrap(),
        "version-three-content\n"
    );
}

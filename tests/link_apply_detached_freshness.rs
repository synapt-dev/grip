//! End-to-end witnesses for detached gripspace freshness through the `gr` binary.
//!
//! These start above manifest resolution because that caller used to erase the
//! detached premise before the freshness guard could inspect it.

use assert_cmd::Command;
use std::path::{Path, PathBuf};
use std::process::Command as StdCommand;
use tempfile::TempDir;

fn git(dir: &Path, args: &[&str]) -> String {
    let out = StdCommand::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {:?} in {}: {}",
        args,
        dir.display(),
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn repo(root: &Path, name: &str, files: &[(&str, &str)]) -> PathBuf {
    let dir = root.join(name);
    std::fs::create_dir_all(&dir).unwrap();
    git(&dir, &["init", "-q", "-b", "main"]);
    git(&dir, &["config", "user.email", "t@e.com"]);
    git(&dir, &["config", "user.name", "t"]);
    // Keep content byte-exact across checkout. The GitHub Windows runner sets
    // core.autocrlf=true globally, so committed `\n` content (e.g. SECTION.md
    // "v1\n") would check out as `\r\n` in gr's materialized CLONE of this repo
    // — and compose copies bytes faithfully (src/files/mod.rs), so OUT.md then
    // reads "v1\r\n" and the content assertions fail. The source repo's own
    // config does not propagate to the clone, so a committed `.gitattributes`
    // (`* -text`, which travels with the repo and overrides autocrlf) is the
    // load-bearing lever; the local config setting covers this repo's own tree.
    git(&dir, &["config", "core.autocrlf", "false"]);
    std::fs::write(dir.join(".gitattributes"), "* -text\n").unwrap();
    for (p, b) in files {
        let full = dir.join(p);
        if let Some(par) = full.parent() {
            std::fs::create_dir_all(par).unwrap();
        }
        std::fs::write(full, b).unwrap();
    }
    git(&dir, &["add", "-A"]);
    git(&dir, &["commit", "-qm", "initial"]);
    dir
}

struct Fx {
    _t: TempDir,
    source: PathBuf,
    workspace: PathBuf,
    space: PathBuf,
}

/// Build a workspace whose gripspace source is materialized at `rev`.
fn setup(rev: &str) -> Fx {
    setup_with_source(rev, |_| {})
}

fn setup_with_source<F>(rev: &str, prepare_source: F) -> Fx
where
    F: FnOnce(&Path),
{
    let t = TempDir::new().unwrap();
    let root = t.path().to_path_buf();
    let source = repo(
        &root,
        "source-space",
        &[
            ("SECTION.md", "v1\n"),
            ("gripspace.yml", "version: 2\nrepos: {}\n"),
        ],
    );
    prepare_source(&source);
    let dummy = repo(&root, "dummy-repo", &[("README.md", "d\n")]);
    let manifest = repo(
        &root,
        "workspace-manifest",
        &[(
            "gripspace.yml",
            &format!(
                r#"version: 2
gripspaces:
  - url: '{}'
    rev: {}
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
                rev,
                root.join("workspace-manifest").display(),
                dummy.display()
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
        "init: {}{}",
        String::from_utf8_lossy(&init.stdout),
        String::from_utf8_lossy(&init.stderr)
    );
    let sync = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert!(
        sync.status.success(),
        "baseline sync: {}{}",
        String::from_utf8_lossy(&sync.stdout),
        String::from_utf8_lossy(&sync.stderr)
    );
    let space = workspace.join(".gitgrip/spaces/source-space");
    Fx {
        _t: t,
        source,
        workspace,
        space,
    }
}

fn apply(ws: &Path) -> (Option<i32>, String) {
    let o = Command::cargo_bin("gr1")
        .unwrap()
        .args(["link", "--apply"])
        .current_dir(ws)
        .output()
        .unwrap();
    (
        o.status.code(),
        format!(
            "{}{}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        ),
    )
}

fn run_sync(ws: &Path) -> (Option<i32>, String) {
    let o = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(ws)
        .output()
        .unwrap();
    (
        o.status.code(),
        format!(
            "{}{}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        ),
    )
}

fn recorded(space: &Path) -> String {
    let o = StdCommand::new("git")
        .args(["config", "--get", "gitgrip.requestedGripspaceRev"])
        .current_dir(space)
        .output()
        .unwrap();
    format!(
        "exit={:?} value={:?}",
        o.status.code(),
        String::from_utf8_lossy(&o.stdout).trim()
    )
}

#[test]
fn link_apply_refuses_an_attached_source_on_the_wrong_configured_branch() {
    let fx = setup("main");
    let out = std::fs::read_to_string(fx.workspace.join("OUT.md")).unwrap();
    assert_eq!(
        out, "v1\n",
        "precondition: the workspace starts from configured main; got {out:?}"
    );
    git(&fx.source, &["branch", "scratch", "HEAD"]);
    git(&fx.space, &["checkout", "-qb", "scratch"]);

    std::fs::write(fx.source.join("SECTION.md"), "v2\n").unwrap();
    git(&fx.source, &["add", "-A"]);
    git(&fx.source, &["commit", "-qm", "advance main"]);

    let (code, diag) = apply(&fx.workspace);
    assert_eq!(code, Some(2), "wrong attached branch must refuse: {diag}");
    assert!(
        diag.contains("is on branch 'scratch', configured revision 'main'"),
        "wrong-branch diagnostic: {diag}"
    );
    assert_eq!(
        git(&fx.space, &["branch", "--show-current"]),
        "scratch",
        "refusal must not silently reattach the source"
    );
    assert_eq!(
        std::fs::read_to_string(fx.workspace.join("OUT.md")).unwrap(),
        "v1\n",
        "refusal must not compose stale content"
    );
}

#[test]
fn control_attached_source_on_the_configured_current_branch_is_accepted() {
    let fx = setup("main");
    assert_eq!(
        git(&fx.space, &["branch", "--show-current"]),
        "main",
        "precondition: attached to configured main"
    );

    let (code, diag) = apply(&fx.workspace);
    assert_eq!(code, Some(0), "current configured branch must pass: {diag}");
    let out = std::fs::read_to_string(fx.workspace.join("OUT.md")).unwrap();
    assert_eq!(out, "v1\n", "composed OUT.md content; got {out:?}");
}

#[test]
fn link_apply_refuses_a_branch_configured_source_that_is_detached() {
    let fx = setup("main");
    eprintln!("[A1] recorded after sync: {}", recorded(&fx.space));
    assert!(
        git(&fx.space, &["branch", "--show-current"]) == "main",
        "precondition: attached to main"
    );
    git(&fx.space, &["checkout", "--detach", "HEAD"]);
    assert!(
        git(&fx.space, &["branch", "--show-current"]).is_empty(),
        "precondition: detached"
    );
    let (code, diag) = apply(&fx.workspace);
    eprintln!("[A1] exit={:?}\n{}", code, diag);
    assert_eq!(code, Some(2), "A1 must refuse");
    assert!(
        diag.contains("detached from configured branch"),
        "A1 diagnostic: {diag}"
    );
    assert!(
        git(&fx.space, &["branch", "--show-current"]).is_empty(),
        "refusal must not silently reattach the source"
    );
}

#[test]
fn link_apply_refuses_a_detached_source_without_recorded_provenance() {
    let fx = setup("main");
    git(
        &fx.space,
        &["config", "--unset", "gitgrip.requestedGripspaceRev"],
    );
    eprintln!("[A2a] recorded after unset: {}", recorded(&fx.space));
    git(&fx.space, &["checkout", "--detach", "HEAD"]);
    let (code, diag) = apply(&fx.workspace);
    eprintln!("[A2a] exit={:?}\n{}", code, diag);
    assert_eq!(code, Some(2), "A2a must refuse");
    assert!(
        diag.contains("provenance is unavailable"),
        "A2a diagnostic: {diag}"
    );
}

#[test]
fn link_apply_refuses_a_detached_source_with_an_unresolvable_named_revision() {
    let fx = setup("main");
    git(
        &fx.space,
        &[
            "config",
            "gitgrip.requestedGripspaceRev",
            "v9.9.9-does-not-exist",
        ],
    );
    git(&fx.space, &["checkout", "--detach", "HEAD"]);
    let (code, diag) = apply(&fx.workspace);
    eprintln!("[A2b] exit={:?}\n{}", code, diag);
    assert_eq!(code, Some(2), "A2b must refuse");
    assert!(
        diag.contains("configured revision"),
        "A2b diagnostic: {diag}"
    );
}

#[test]
fn an_unresolvable_all_hex_commit_prefix_names_an_operator_remedy() {
    let fx = setup("main");
    git(
        &fx.space,
        &[
            "config",
            "gitgrip.requestedGripspaceRev",
            "0000000000000000000000000000000000000000",
        ],
    );
    git(&fx.space, &["checkout", "--detach", "HEAD"]);
    let (code, diag) = apply(&fx.workspace);
    assert_eq!(code, Some(2), "unresolvable commit prefix must refuse");
    assert!(
        diag.contains("Use a longer commit id if the prefix is ambiguous")
            && diag.contains("correct the configured revision"),
        "diagnostic must name remedies that can change the result: {diag}"
    );
}

fn assert_explicit_sha_pin_is_accepted(pin_len: usize) {
    // Build with rev = the source's initial commit SHA.
    let t = TempDir::new().unwrap();
    let root = t.path().to_path_buf();
    let source = repo(
        &root,
        "source-space",
        &[
            ("SECTION.md", "v1\n"),
            ("gripspace.yml", "version: 2\nrepos: {}\n"),
        ],
    );
    let full_pin = git(&source, &["rev-parse", "HEAD"]);
    let pin = &full_pin[..pin_len];
    let dummy = repo(&root, "dummy-repo", &[("README.md", "d\n")]);
    let manifest = repo(
        &root,
        "workspace-manifest",
        &[(
            "gripspace.yml",
            &format!(
                r#"version: 2
gripspaces:
  - url: '{}'
    rev: {}
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
                pin,
                root.join("workspace-manifest").display(),
                dummy.display()
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
        "init: {}{}",
        String::from_utf8_lossy(&init.stdout),
        String::from_utf8_lossy(&init.stderr)
    );
    let sync = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(&workspace)
        .output()
        .unwrap();
    assert!(
        sync.status.success(),
        "sync: {}{}",
        String::from_utf8_lossy(&sync.stdout),
        String::from_utf8_lossy(&sync.stderr)
    );
    let space = workspace.join(".gitgrip/spaces/source-space");
    eprintln!("[A3/{pin_len}] recorded: {}", recorded(&space));
    assert!(
        git(&space, &["branch", "--show-current"]).is_empty(),
        "SHA pin must be detached"
    );
    // Advance upstream: an immutable commit pin is NOT stale by definition.
    std::fs::write(source.join("SECTION.md"), "v2\n").unwrap();
    git(&source, &["add", "-A"]);
    git(&source, &["commit", "-qm", "advance"]);
    let (code, diag) = apply(&workspace);
    eprintln!("[A3/{pin_len}] exit={:?}\n{}", code, diag);
    assert_eq!(code, Some(0), "A3 control must be ACCEPTED: {diag}");
}

#[test]
fn control_explicit_full_sha_pin_is_accepted() {
    assert_explicit_sha_pin_is_accepted(40);
}

#[test]
fn explicit_short_sha_pin_is_accepted() {
    assert_explicit_sha_pin_is_accepted(8);
}

#[test]
fn moved_tag_is_refused_until_sync_updates_the_managed_clone() {
    let fx = setup_with_source("release", |source| {
        git(source, &["tag", "release"]);
    });
    let source = &fx.source;
    let workspace = &fx.workspace;
    let space = &fx.space;
    eprintln!("[A4] recorded: {}", recorded(&space));
    let before = git(&space, &["rev-parse", "HEAD"]);
    // Upstream moves the tag to new content.
    std::fs::write(source.join("SECTION.md"), "v2-MOVED-TAG\n").unwrap();
    git(&source, &["add", "-A"]);
    git(&source, &["commit", "-qm", "advance"]);
    git(&source, &["tag", "-f", "release"]);
    let upstream = git(&source, &["rev-parse", "release"]);
    assert_ne!(before, upstream, "fixture: tag must have moved");
    let (code, diag) = apply(&workspace);
    let after = git(&space, &["rev-parse", "HEAD"]);
    let out = std::fs::read_to_string(workspace.join("OUT.md")).unwrap_or_default();
    eprintln!("[A4] local_before={before} upstream_tag={upstream} local_after={after}");
    eprintln!("[A4] exit={:?} OUT.md={:?}\n{}", code, out, diag);
    eprintln!(
        "[A4] local tag after fetch: {}",
        git(&space, &["rev-parse", "release"])
    );
    assert_eq!(code, Some(2), "moved upstream tag must refuse: {diag}");
    assert!(
        diag.contains("does not match configured revision 'release'"),
        "moved-tag diagnostic: {diag}"
    );
    assert_eq!(after, before, "refusal must not move the managed clone");
    assert_eq!(
        out, "v1\n",
        "refusal must not compose stale content again; got {out:?}"
    );

    let (sync_code, sync_diag) = run_sync(&workspace);
    assert_eq!(sync_code, Some(0), "gr sync must recover: {sync_diag}");
    assert_eq!(
        git(&space, &["rev-parse", "HEAD"]),
        upstream,
        "sync must materialize the moved upstream tag"
    );
    assert_eq!(
        git(&space, &["rev-parse", "release"]),
        upstream,
        "sync must force-update the managed local tag"
    );

    let (apply_code, apply_diag) = apply(&workspace);
    assert_eq!(
        apply_code,
        Some(0),
        "apply after recovery must succeed: {apply_diag}"
    );
    let recovered = std::fs::read_to_string(workspace.join("OUT.md")).unwrap();
    assert_eq!(
        recovered, "v2-MOVED-TAG\n",
        "recovered apply must compose the upstream tag content; got {recovered:?}"
    );
}

#[test]
fn an_all_hex_tag_deleted_from_origin_is_not_reinterpreted_as_a_commit_prefix() {
    let fx = setup_with_source("cafe", |source| {
        git(source, &["tag", "cafe"]);
    });
    let before = git(&fx.space, &["rev-parse", "HEAD"]);
    assert_eq!(
        git(&fx.space, &["rev-parse", "cafe"]),
        before,
        "fixture: the managed clone must retain its local tag"
    );

    git(&fx.source, &["tag", "-d", "cafe"]);
    let (code, diag) = apply(&fx.workspace);
    assert_eq!(
        code,
        Some(2),
        "a tag absent from origin must not certify through its stale local ref: {diag}"
    );
    assert_eq!(
        git(&fx.space, &["rev-parse", "HEAD"]),
        before,
        "refusal must not move the managed clone"
    );
    let out = std::fs::read_to_string(fx.workspace.join("OUT.md")).unwrap();
    assert_eq!(
        out, "v1\n",
        "refusal must not compose again from the stale local tag; got {out:?}"
    );
}

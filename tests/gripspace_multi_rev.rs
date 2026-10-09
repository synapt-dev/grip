//! Command-level witnesses: multiple gripspaces from ONE repository at
//! DIFFERENT revisions must coexist as first-class spaces.
//!
//! *** THE DEFECT THESE WERE BORN RED AGAINST (2026-08-12). ***
//!
//! `gripspace_identity()` already knows a gripspace is `url#rev`. But
//! `resolve_space_name()` derived the space directory from the URL alone, and
//! the reuse check asked only "same remote?" — so the second gripspace of a
//! repo silently REUSED the first rev's clone. Measured live: a manifest
//! declaring `rev: standards` and `rev: api` from one repo materialized ONE
//! space at `standards`; the `api` binding resolved to nothing; compose
//! printed a warning and then a success line.
//!
//! One repo hosting many gripspaces as orphan-branch roots is the tutorial's
//! and the product's core shape ("the whole point is multiple gripspaces" —
//! the ruling that opened this fix). These witnesses drive the real binary
//! end to end, because the naming function, the reuse check, the sync
//! mapping, and the compose binding all have to agree before the feature
//! exists — a green unit test on the naming helper alone proves none of that.

use assert_cmd::Command;
use std::path::Path;
use std::process::Command as StdCommand;
use tempfile::TempDir;

fn git(dir: &Path, args: &[&str]) {
    let out = StdCommand::new("git")
        .args(args)
        .current_dir(dir)
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t")
        .output()
        .unwrap_or_else(|e| panic!("git {args:?} failed to spawn: {e}"));
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// One bare repo, three orphan-branch roots:
///   `main`      — the workspace manifest + a local fragment
///   `standards` — fragment MARKER-STANDARDS-a83f
///   `extras`    — fragment MARKER-EXTRAS-b17c
/// The manifest declares the SAME url as two gripspaces at the two revs and
/// composes one CLAUDE.md from both plus the local part.
fn build_one_repo_two_gripspace_origin(root: &Path, bare_initial_branch: &str) -> String {
    let bare = root.join("frag.git");
    let initial_branch = format!("--initial-branch={bare_initial_branch}");
    git(root, &["init", "--bare", &initial_branch, "frag.git"]);
    // Build the file:// URL through the production helper, not a hand-rolled
    // copy. This fixture previously inlined the URL logic, and that copy still
    // carried the pre-fix bug std::fs::canonicalize returns a verbatim
    // `\\?\C:\…` path on Windows, which the naive `\`→`/` + `nth(1)==':'` check
    // mangled into `file:////?/C:/…` — an unclonable URL. It failed only on the
    // Windows runner (canonicalize yields no verbatim prefix off Windows), which
    // is exactly why the inline copy survived when the production function was
    // fixed. One implementation, exercised on every platform, cannot drift again.
    let url = gitgrip::core::gripspace::path_to_file_url(&bare);

    let w = root.join("seed");
    std::fs::create_dir_all(&w).unwrap();
    git(&w, &["init"]);

    git(&w, &["checkout", "-b", "standards"]);
    std::fs::write(w.join("CONVENTIONS.md"), "MARKER-STANDARDS-a83f\n").unwrap();
    std::fs::write(w.join("gripspace.yml"), "version: 2\n").unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "standards root"]);

    git(&w, &["checkout", "--orphan", "extras"]);
    git(&w, &["rm", "-rf", "."]);
    std::fs::write(w.join("EXTRA.md"), "MARKER-EXTRAS-b17c\n").unwrap();
    std::fs::write(w.join("gripspace.yml"), "version: 2\n").unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "extras root"]);

    git(&w, &["checkout", "--orphan", "main"]);
    git(&w, &["rm", "-rf", "."]);
    std::fs::write(w.join("PROJECT.md"), "MARKER-LOCAL-c29d\n").unwrap();
    std::fs::write(
        w.join("gripspace.yml"),
        format!(
            r#"version: 2
manifest:
  url: {url}
  composefile:
    - dest: CLAUDE.md
      separator: "\n"
      parts:
        - gripspace: frag
          src: CONVENTIONS.md
        - gripspace: frag-extras
          src: EXTRA.md
        - src: PROJECT.md
gripspaces:
  - url: {url}
    rev: standards
  - url: {url}
    rev: extras
repos:
  extras-checkout:
    url: {url}
    path: ./extras-checkout
    revision: extras
"#
        ),
    )
    .unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "manifest root"]);
    git(&w, &["push", &url, "standards", "extras", "main"]);
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);

    url
}

fn gr() -> Command {
    let mut c = Command::cargo_bin("gr1").expect("gr binary");
    c.env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t");
    c
}

/// Locate the workspace directory `gr init` created under `parent`.
fn workspace_dir(parent: &Path) -> std::path::PathBuf {
    std::fs::read_dir(parent)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .find(|p| p.is_dir() && p.join(".gitgrip").exists())
        .expect("gr init produced a workspace directory")
}

#[test]
fn two_revs_of_one_repo_materialize_as_two_spaces_and_both_compose() {
    let tmp = TempDir::new().unwrap();
    let url = build_one_repo_two_gripspace_origin(tmp.path(), "main");
    let ws_parent = tmp.path().join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();

    let assert = gr().args(["init", &url]).current_dir(&ws_parent).assert();
    let out = assert.get_output().clone();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    assert.success();

    let ws = workspace_dir(&ws_parent);
    let spaces = ws.join(".gitgrip").join("spaces");

    // Both revs exist as first-class spaces, each carrying ITS branch's file.
    assert!(
        spaces.join("frag").join("CONVENTIONS.md").exists(),
        "first-declared rev keeps the base name and its content; spaces present: {:?}",
        std::fs::read_dir(&spaces)
            .map(|d| d
                .filter_map(|e| e.ok())
                .map(|e| e.file_name())
                .collect::<Vec<_>>())
            .unwrap_or_default()
    );
    assert!(
        spaces.join("frag-extras").join("EXTRA.md").exists(),
        "second rev materializes under the deterministic name '<base>-<rev>'"
    );

    // The composed file carries fragments from BOTH branches of the ONE repo.
    let claude = std::fs::read_to_string(ws.join("CLAUDE.md"))
        .expect("CLAUDE.md composed at workspace root");
    assert!(
        claude.contains("MARKER-STANDARDS-a83f"),
        "standards fragment present"
    );
    assert!(
        claude.contains("MARKER-EXTRAS-b17c"),
        "extras fragment present"
    );
    assert!(
        claude.contains("MARKER-LOCAL-c29d"),
        "local fragment present"
    );

    // The disambiguation is NAMED, on stderr — an invented space name that
    // appears in no manifest must not be invented silently.
    assert!(
        stderr.contains("frag-extras"),
        "stderr names the deterministic space name; stderr was:\n{stderr}"
    );
}

#[test]
fn sync_reuses_both_spaces_instead_of_proliferating() {
    let tmp = TempDir::new().unwrap();
    let url = build_one_repo_two_gripspace_origin(tmp.path(), "main");
    let ws_parent = tmp.path().join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();
    gr().args(["init", &url])
        .current_dir(&ws_parent)
        .assert()
        .success();
    let ws = workspace_dir(&ws_parent);

    gr().arg("sync").current_dir(&ws).assert().success();
    gr().arg("sync").current_dir(&ws).assert().success();

    let spaces = ws.join(".gitgrip").join("spaces");
    let named: Vec<String> = std::fs::read_dir(&spaces)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("frag"))
        .collect();
    let mut sorted = named.clone();
    sorted.sort();
    assert_eq!(
        sorted,
        vec!["frag".to_string(), "frag-extras".to_string()],
        "repeat syncs reuse the two spaces — no frag-2/frag-3 proliferation"
    );

    // Idempotent compose still carries both fragments after repeat syncs.
    let claude = std::fs::read_to_string(ws.join("CLAUDE.md")).unwrap();
    assert!(claude.contains("MARKER-STANDARDS-a83f"));
    assert!(claude.contains("MARKER-EXTRAS-b17c"));
}

/// Regression control for the CI environment: a bare repository initialized
/// with `master` as its default still clones the pushed `main` manifest because
/// the fixture sets the origin's symbolic HEAD explicitly.
#[test]
fn fixture_origin_is_cloneable_when_bare_git_defaults_to_master() {
    let tmp = TempDir::new().unwrap();
    let url = build_one_repo_two_gripspace_origin(tmp.path(), "master");
    let ws_parent = tmp.path().join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();

    gr().args(["init", &url])
        .current_dir(&ws_parent)
        .assert()
        .success();

    let ws = workspace_dir(&ws_parent);
    let claude = std::fs::read_to_string(ws.join("CLAUDE.md"))
        .expect("main manifest composes after cloning the explicit origin HEAD");
    assert!(claude.contains("MARKER-LOCAL-c29d"));
}

/// Discriminating control: the fix must key on url+rev, not "always allocate a
/// new space". The SAME url at the SAME rev declared twice still collapses to
/// one space — if this test fails while the others pass, the implementation
/// went to per-entry allocation, which is right by accident and wrong by design.
#[test]
fn same_url_same_rev_twice_still_yields_one_space() {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    let bare = root.join("frag.git");
    git(
        root,
        &["init", "--bare", "--initial-branch=master", "frag.git"],
    );
    // Through the production helper: a raw `file://{display}` is `file://C:\…`
    // on Windows, and gripspace_name() splits on `/` only, so the name never
    // resolves to "frag" and the dedup assertion sees an empty set. The helper
    // yields forward-slash `file:///C:/…`. No-op off Windows.
    let url = gitgrip::core::gripspace::path_to_file_url(&bare);

    let w = root.join("seed");
    std::fs::create_dir_all(&w).unwrap();
    git(&w, &["init"]);
    git(&w, &["checkout", "-b", "standards"]);
    std::fs::write(w.join("CONVENTIONS.md"), "MARKER-STANDARDS-a83f\n").unwrap();
    std::fs::write(w.join("gripspace.yml"), "version: 2\n").unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "standards root"]);
    git(&w, &["checkout", "--orphan", "main"]);
    git(&w, &["rm", "-rf", "."]);
    std::fs::write(
        w.join("gripspace.yml"),
        format!(
            r#"version: 2
manifest:
  url: {url}
gripspaces:
  - url: {url}
    rev: standards
  - url: {url}
    rev: standards
repos:
  standards-checkout:
    url: {url}
    path: ./standards-checkout
    revision: standards
"#
        ),
    )
    .unwrap();
    git(&w, &["add", "-A"]);
    git(&w, &["commit", "-m", "manifest"]);
    git(&w, &["push", &url, "standards", "main"]);
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);

    let ws_parent = root.join("ws");
    std::fs::create_dir_all(&ws_parent).unwrap();
    gr().args(["init", &url])
        .current_dir(&ws_parent)
        .assert()
        .success();
    let ws = workspace_dir(&ws_parent);

    let spaces = ws.join(".gitgrip").join("spaces");
    let named: Vec<String> = std::fs::read_dir(&spaces)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("frag"))
        .collect();
    assert_eq!(
        named,
        vec!["frag".to_string()],
        "identical url+rev dedups to one space — allocation is by identity, not by entry"
    );
}

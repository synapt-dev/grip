//! Composition ACROSS gripspace layers, exercised through the real seam.
//!
//! *** WHY THIS FILE EXISTS, AND IT IS A REVIEW FINDING, NOT A PREFERENCE. ***
//!
//! The unit tests for this feature call `merge_composefiles_in_layer_order`
//! and `process_composefiles` DIRECTLY. Both are downstream of the thing that
//! actually makes layered composition work: the point inside
//! `resolve_gripspace_recursive` where an included manifest's bare part is
//! rewritten to `gripspace: <resolved-dir>` and carried into the merged
//! manifest.
//!
//! A reviewer pointed out that a mutation removing that rewrite, or dropping
//! included composefiles before the helper is reached, leaves every one of
//! those unit tests GREEN. They pin the functions; they do not pin the USE.
//! A manual end-to-end run is evidence about one head, not a regression
//! witness that survives the next refactor.
//!
//! So this drives the real `gr` binary through init and sync, and it is built
//! to go RED on either mutation:
//!
//!   - remove the REBASE  -> base's bare `src: SHARED.md` resolves
//!     manifest-relative again and reads the root's DECOY file, so the decoy
//!     content appears and the layer's own content does not
//!   - remove the CARRY   -> the ancestors' parts never reach the merge at all
//!
//! The decoy is the load-bearing part. Without a same-named file at the old
//! resolution path, a broken rebase would simply find nothing, and "absent"
//! is indistinguishable from "the layer contributed nothing" — which is the
//! bug this feature fixes, passing itself off as the fix.

use assert_cmd::Command;
use std::path::{Path, PathBuf};
use std::process::Command as StdCommand;
use tempfile::TempDir;

fn git(dir: &Path, args: &[&str]) {
    let out = StdCommand::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {:?} failed in {:?}: {}",
        args,
        dir,
        String::from_utf8_lossy(&out.stderr)
    );
}

/// A local git repo holding `files`, usable as a gripspace URL.
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
    git(&dir, &["commit", "-qm", "init"]);
    dir
}

#[test]
fn composition_accumulates_across_layers_and_prefers_the_declaring_layers_file() {
    let origin = TempDir::new().unwrap();
    let root = origin.path();

    // THE DEEPEST ANCESTOR. Its part is BARE — the case that must be rebased.
    let base = repo(
        root,
        "base",
        &[
            ("SHARED.md", "FROM BASE"),
            (
                "gripspace.yml",
                "repos: {}\n\
                 manifest:\n  \
                   url: \"\"\n  \
                   composefile:\n    \
                     - dest: CLAUDE.md\n      \
                       parts:\n        \
                         - src: SHARED.md\n",
            ),
        ],
    );

    // THE MIDDLE LAYER, also bare, and it uses THE SAME FILENAME as base and
    // as the root decoy. Same name across all three is deliberate: it is what
    // makes a wrong resolution produce wrong CONTENT rather than no content.
    let standards = repo(
        root,
        "standards",
        &[
            ("SHARED.md", "FROM STANDARDS"),
            (
                "gripspace.yml",
                &format!(
                    "repos: {{}}\n\
                     gripspaces:\n  \
                       - url: {}\n\
                     manifest:\n  \
                       url: \"\"\n  \
                       composefile:\n    \
                         - dest: CLAUDE.md\n      \
                           parts:\n        \
                             - src: SHARED.md\n",
                    base.display()
                ),
            ),
        ],
    );

    // THE ROOT, carrying a DECOY at the manifest-relative path the ancestors'
    // bare parts USED to resolve to.
    let top = repo(
        root,
        "top",
        &[
            ("SHARED.md", "DECOY FROM ROOT MANIFEST"),
            ("TOP.md", "FROM TOP"),
            (
                "gripspace.yml",
                &format!(
                    "repos: {{}}\n\
                     gripspaces:\n  \
                       - url: {}\n\
                     manifest:\n  \
                       url: \"\"\n  \
                       composefile:\n    \
                         - dest: CLAUDE.md\n      \
                           parts:\n        \
                             - src: TOP.md\n",
                    standards.display()
                ),
            ),
        ],
    );

    let ws = TempDir::new().unwrap();
    let init = Command::cargo_bin("gr1")
        .unwrap()
        .args(["init", top.to_str().unwrap()])
        .current_dir(ws.path())
        .output()
        .unwrap();
    assert!(
        init.status.success(),
        "PRECONDITION FAILED: gr init did not succeed, so anything below is a \
         fact about the harness rather than about gr.\nstdout: {}\nstderr: {}",
        String::from_utf8_lossy(&init.stdout),
        String::from_utf8_lossy(&init.stderr),
    );
    let workspace = ws.path().join("workspace");

    let sync = Command::cargo_bin("gr1")
        .unwrap()
        .arg("sync")
        .current_dir(&workspace)
        .output()
        .unwrap();

    // PRECONDITION: gripspace resolution must not have degraded to a warning.
    // Without this, a fixture mistake reads as a feature failure — resolution
    // failure currently warns while sync still reports success.
    let stderr = String::from_utf8_lossy(&sync.stderr);
    let stdout = String::from_utf8_lossy(&sync.stdout);
    assert!(
        !stdout.contains("Gripspace resolution failed")
            && !stderr.contains("Gripspace resolution failed"),
        "PRECONDITION FAILED: gripspace resolution failed, so this test is \
         about the fixture rather than about composition.\nstdout: {stdout}\nstderr: {stderr}"
    );

    // PRECONDITION: every layer materialized, or "missing content" would just
    // mean "the layer was never fetched".
    for layer in ["base", "standards"] {
        assert!(
            workspace.join(".gitgrip/spaces").join(layer).is_dir(),
            "PRECONDITION FAILED: gripspace '{layer}' did not materialize"
        );
    }

    let composed_path = workspace.join("CLAUDE.md");
    assert!(
        composed_path.is_file(),
        "no composed file was produced at all.\nstdout: {stdout}\nstderr: {stderr}"
    );
    let composed = std::fs::read_to_string(&composed_path).unwrap();

    // THE CARRY: ancestors' composefiles reached the merge.
    assert!(
        composed.contains("FROM BASE"),
        "the deepest ancestor's composefile was dropped.\ngot:\n{composed}"
    );
    assert!(
        composed.contains("FROM STANDARDS"),
        "the middle layer's composefile was dropped.\ngot:\n{composed}"
    );
    assert!(
        composed.contains("FROM TOP"),
        "the root's own part is missing"
    );

    // THE REBASE: a bare part resolved against the layer that WROTE it, not
    // against the root manifest. This is what the decoy exists to catch.
    assert!(
        !composed.contains("DECOY"),
        "a bare part from an included gripspace resolved against the ROOT \
         manifest and picked up the decoy.\ngot:\n{composed}"
    );

    // THE ORDER: ancestors first, local last, checked at this same boundary.
    let base_at = composed.find("FROM BASE").unwrap();
    let std_at = composed.find("FROM STANDARDS").unwrap();
    let top_at = composed.find("FROM TOP").unwrap();
    assert!(
        base_at < std_at && std_at < top_at,
        "layers are not ancestors-first.\ngot:\n{composed}"
    );
}

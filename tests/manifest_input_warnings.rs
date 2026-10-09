//! Command-level witnesses for manifest input that would otherwise be discarded
//! in silence.
//!
//! *** WHY THESE ARE NOT UNIT TESTS, WHICH IS THE WHOLE POINT. ***
//!
//! The first attempt at covering this shipped four unit tests against
//! `Manifest::unknown_top_level_keys`. Review blocked it: every one exercised
//! the HELPER, none drove `filter_repos` with a bad URL, and none proved the
//! warning reaches STDERR through a real command. So a revert to a silent
//! `filter_map`, or a warning quietly moved back to stdout where `--json`
//! consumers would swallow it, left all four GREEN.
//!
//! The author had just counted call sites to find the right chokepoint
//! (`parse_raw`, not `load`) and then tested the helper anyway. VERIFYING WHERE
//! YOUR CODE GOES AND VERIFYING THROUGH WHERE IT GOES ARE TWO DIFFERENT ACTS,
//! and the second is easier to skip because the first felt like diligence.
//!
//! So these drive the real binary, and assert on the STREAM as well as the
//! text, because the routing is half the contract: a correctness warning on
//! stdout is swallowed by redirection and corrupts `--json`.

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

/// A bare origin carrying a manifest on `main` and one content branch `api`.
///
/// Returns (tempdir, file:// url of the bare repo).
fn origin_with_manifest(manifest_body: &str) -> (TempDir, String) {
    let temp = TempDir::new().unwrap();
    let bare = temp.path().join("origin.git");
    std::fs::create_dir_all(&bare).unwrap();
    git(temp.path(), &["init", "-q", "--bare", "origin.git"]);
    // Pin the bare repo's default branch to `main` regardless of the runner's
    // init.defaultBranch. The manifest is pushed to `main`; on a runner whose
    // default is `master` (git's built-in default), `gr init` clones and checks
    // out the empty/absent default branch and reports "No workspace manifest
    // found". That is the CI-only failure of these tests (green locally where
    // init.defaultBranch=main); reproduced with GIT_CONFIG init.defaultBranch=master.
    git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);
    // Through the production helper so the URL is forward-slash `file:///C:/…`
    // on Windows, not `file://C:\…` (a raw display()), which gripspace_name()
    // cannot parse. No-op off Windows. Same class as gripspace_multi_rev.
    let url = gitgrip::core::gripspace::path_to_file_url(&bare);

    let work = temp.path().join("work");
    std::fs::create_dir_all(&work).unwrap();
    git(&work, &["init", "-q"]);
    git(&work, &["remote", "add", "origin", bare.to_str().unwrap()]);

    // content branch
    git(&work, &["checkout", "-q", "--orphan", "api"]);
    std::fs::write(work.join("API.md"), "api\n").unwrap();
    git(&work, &["add", "-A"]);
    git(&work, &["commit", "-qm", "api"]);
    git(&work, &["push", "-q", "origin", "api"]);

    // manifest branch
    git(&work, &["checkout", "-q", "--orphan", "main"]);
    let _ = StdCommand::new("git")
        .args(["rm", "-rqf", "."])
        .current_dir(&work)
        .output();
    std::fs::write(work.join("PROJECT.md"), "project\n").unwrap();
    std::fs::write(
        work.join("gripspace.yml"),
        manifest_body.replace("__URL__", &url),
    )
    .unwrap();
    git(&work, &["add", "-A"]);
    git(&work, &["commit", "-qm", "manifest"]);
    git(&work, &["push", "-qf", "origin", "main"]);

    (temp, url)
}

/// `gr init <url>` then `gr sync` inside the created workspace. Returns sync's output.
fn init_then_sync(url: &str, extra: &[&str]) -> (TempDir, std::process::Output) {
    let ws = TempDir::new().unwrap();
    let init = Command::cargo_bin("gr1")
        .unwrap()
        .args(["init", url])
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
    assert!(
        workspace.is_dir(),
        "PRECONDITION FAILED: gr init created no workspace/ directory"
    );

    let mut args = vec!["sync"];
    args.extend_from_slice(extra);
    let out = Command::cargo_bin("gr1")
        .unwrap()
        .args(&args)
        .current_dir(&workspace)
        .output()
        .unwrap();
    (ws, out)
}

const VALID_MANIFEST: &str = r#"version: 2
repos:
  api: { url: "__URL__", path: ./api, revision: api }
"#;

/// A bare filesystem path instead of a `file://` URL. `gr init` clones from it
/// happily; `parse_git_url` cannot read it, so `filter_repos` used to drop the
/// repo without a word and every downstream count printed zero, styled as
/// success. That silence produced a defect report against repo registration,
/// which was never broken.
const UNPARSEABLE_URL_MANIFEST: &str = r#"version: 2
repos:
  api: { url: "/tmp/definitely-not-a-url/origin.git", path: ./api, revision: api }
"#;

/// `composefile` at the TOP level. It belongs under `manifest:`, serde discards
/// unknown keys in silence, and the block vanishes at exit 0.
const MISPLACED_KEY_MANIFEST: &str = r#"version: 2
repos:
  api: { url: "__URL__", path: ./api, revision: api }
composefile:
  - dest: CLAUDE.md
    parts:
      - src: PROJECT.md
"#;

#[test]
fn sync_names_a_repo_it_could_not_resolve_and_says_so_on_stderr() {
    let (_origin, url) = origin_with_manifest(UNPARSEABLE_URL_MANIFEST);
    let (_ws, out) = init_then_sync(&url, &[]);

    let stderr = String::from_utf8_lossy(&out.stderr);
    let stdout = String::from_utf8_lossy(&out.stdout);

    // THE REPO, so the user knows which declaration was ignored.
    assert!(
        stderr.contains("api"),
        "stderr must name the dropped repo.\nstderr: {stderr}"
    );
    // THE REASON, because "excluded" without a cause is not actionable.
    assert!(
        stderr.contains("could not be parsed"),
        "stderr must give the reason.\nstderr: {stderr}"
    );
    // THE REMEDY, which is the whole difference between a warning and a fix.
    assert!(
        stderr.contains("file://"),
        "stderr must point at the remedy.\nstderr: {stderr}"
    );
    // AND THE ROUTING IS HALF THE CONTRACT. On stdout this warning is swallowed
    // by redirection and corrupts --json; that is why the stream is asserted
    // rather than just the text.
    assert!(
        !stdout.contains("could not be parsed"),
        "the warning must not be on stdout.\nstdout: {stdout}"
    );
}

#[test]
fn sync_names_a_misplaced_manifest_key_and_says_where_it_belongs() {
    let (_origin, url) = origin_with_manifest(MISPLACED_KEY_MANIFEST);
    let (_ws, out) = init_then_sync(&url, &[]);

    let stderr = String::from_utf8_lossy(&out.stderr);
    let stdout = String::from_utf8_lossy(&out.stdout);

    assert!(
        stderr.contains("composefile"),
        "stderr must name the ignored key.\nstderr: {stderr}"
    );
    assert!(
        stderr.contains("manifest.composefile"),
        "misplacement is the common case, so the warning must name where the \
         key belongs.\nstderr: {stderr}"
    );
    assert!(
        !stdout.contains("manifest.composefile"),
        "the warning must not be on stdout.\nstdout: {stdout}"
    );
}

#[test]
fn a_valid_manifest_warns_on_neither_stream() {
    // *** THE CONTROL THAT MATTERS MOST. ***
    //
    // A warning that fires on valid input is worse than no warning, because it
    // trains readers to ignore the channel -- which is the defect being fixed,
    // one layer up. Without this, both tests above would still pass on an
    // implementation that warned about everything.
    let (_origin, url) = origin_with_manifest(VALID_MANIFEST);
    let (_ws, out) = init_then_sync(&url, &[]);

    let stderr = String::from_utf8_lossy(&out.stderr);
    let stdout = String::from_utf8_lossy(&out.stdout);

    assert!(
        out.status.success(),
        "a valid manifest must sync cleanly.\nstdout: {stdout}\nstderr: {stderr}"
    );
    assert!(
        !stderr.contains("IGNORED"),
        "valid manifest produced an unknown-key warning.\nstderr: {stderr}"
    );
    assert!(
        !stderr.contains("EXCLUDED"),
        "valid manifest produced a dropped-repo warning.\nstderr: {stderr}"
    );
    // Positive half: the repo really did resolve, so the silence above is a
    // clean run rather than a run that did nothing.
    assert!(
        stdout.contains("api") || stdout.contains("1 repositor"),
        "the valid repo should appear in normal output.\nstdout: {stdout}"
    );
}

#[test]
fn a_warning_never_enters_the_json_stdout_stream() {
    // MY contract, narrowly: whatever else is on stdout, the warning is not.
    // That is the half this change owns, and it is asserted in --json mode
    // because that is where stdout pollution actually costs a consumer.
    let (_origin, url) = origin_with_manifest(UNPARSEABLE_URL_MANIFEST);
    let (_ws, out) = init_then_sync(&url, &["--json"]);

    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);

    // PRECONDITION: the warning actually fired, or this asserts nothing.
    assert!(
        stderr.contains("EXCLUDED"),
        "the warning did not fire, so this test proves nothing.\nstderr: {stderr}"
    );
    assert!(
        !stdout.contains("EXCLUDED") && !stdout.contains("could not be parsed"),
        "the warning leaked into --json stdout.\nstdout: {stdout}"
    );
}

#[test]
#[ignore = "PRE-EXISTING defect, not introduced here; see the body. Un-ignore \
            when gr's warning/success output is routed off stdout."]
fn json_stdout_should_be_parseable_json_and_currently_is_not() {
    // *** A DEFECT THIS TEST FOUND, RECORDED RATHER THAN HIDDEN. ***
    //
    // `gr sync --json` prints a human success line BEFORE the JSON document:
    //
    //     ✓ Applied 0 link(s)
    //     { "success": true, ... }
    //
    // so stdout is not parseable JSON and every machine consumer of `--json`
    // is already broken. This is INDEPENDENT of the warning change in this
    // branch -- it reproduces without any manifest defect at all -- and it is
    // an instance of the same root cause: `Output` writes through `println!`,
    // at ~124 call sites across the tree, so every "warning" and success line
    // lands on stdout.
    //
    // Deliberately NOT fixed here. Moving 124 call sites off stdout changes
    // observable output everywhere and is its own change with its own gate;
    // folding it into a manifest-parsing fix would make both unreviewable.
    //
    // Left as an ignored test rather than a comment because a comment is not
    // runnable and cannot tell you when it stops being true. Un-ignore it and
    // it becomes the acceptance criterion for that change.
    let (_origin, url) = origin_with_manifest(VALID_MANIFEST);
    let (_ws, out) = init_then_sync(&url, &["--json"]);

    let stdout = String::from_utf8_lossy(&out.stdout);
    serde_json::from_str::<serde_json::Value>(stdout.trim())
        .unwrap_or_else(|e| panic!("--json stdout is not parseable JSON ({e}).\nstdout: {stdout}"));
}

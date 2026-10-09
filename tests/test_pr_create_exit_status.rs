//! `gr pr create` must not report success after printing its own failure.
//!
//! `run_pr_create` collected per-repo failures into `all_failed_repos`, printed
//! `Failed to create N PR(s):` with every one of them, and then returned
//! `Ok(())`. Anything gating on the exit status -- a shell script, CI, an agent
//! deciding whether to continue -- read that as success while the command was
//! saying the opposite on stdout.
//!
//! The predicate was never missing. Four lines above the return, the `--json`
//! branch already computes `success: !created.is_empty() && failed.is_empty()`
//! and serializes it. The truth was computed and then discarded, exactly as the
//! merge base was in #917.
//!
//! Scope, deliberately narrow (grip#886 is an umbrella over nine verbs): this
//! covers ONLY "failures were reported and the exit status disagreed." The
//! zero-created/zero-failed no-op still exits 0 and is left alone -- that is
//! #804/#836/#839's question, and answering it here would change the exit
//! status of a successful `--dry-run`-shaped run without a witness for it.

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;
use common::mock_platform::{
    mock_branch_exists, mock_create_pr, mock_create_pr_validation_error, point_repo_at_mock,
    setup_github_mock,
};
use wiremock::http::Method;

fn repo_ahead_of(ws: &common::fixtures::WorkspaceFixture, repo: &str, base: &str, feature: &str) {
    let path = ws.repo_path(repo);
    git_helpers::create_branch(&path, base);
    git_helpers::commit_file(&path, "base.txt", "base", "Add base");
    git_helpers::push_branch(&path, "origin", base);
    git_helpers::create_branch(&path, feature);
    git_helpers::commit_file(&path, "feature.txt", "feature", "Add feature");
}

/// THE WITNESS. The platform refuses the PR; the command reports the failure
/// and must not simultaneously claim success through its exit status.
#[tokio::test]
async fn a_reported_failure_must_not_exit_zero() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("frontend").build();
    let mut manifest = ws.load_manifest();
    manifest.settings.target = Some("dev".to_string());

    repo_ahead_of(&ws, "frontend", "dev", "feat/thing");
    point_repo_at_mock(&mut manifest, "frontend", &server);

    mock_branch_exists(&server, "owner", "repo", "dev").await;
    mock_create_pr_validation_error(&server).await;

    let filter = vec!["frontend".to_string()];
    let result = gitgrip::cli::commands::pr::run_pr_create(
        &ws.workspace_root,
        &manifest,
        Some("Add feature"),
        None,
        false,
        false,
        false,
        Some(&filter),
        None,
        false,
    )
    .await;

    // Control: the command must actually have ATTEMPTED the creation. Without
    // this, a command that skipped the repo entirely -- never reaching the
    // failure path at all -- would satisfy the assertion below for the wrong
    // reason, and the test would be pinning a no-op.
    let requests = server.received_requests().await.unwrap();
    let posts = requests
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/pulls"))
        .count();
    assert!(
        posts >= 1,
        "control: the command must attempt the PR before it can report a failure; saw {posts} POSTs"
    );

    assert!(
        result.is_err(),
        "a run that printed 'Failed to create 1 PR(s)' returned Ok -- any caller \
         gating on the exit status reads the command's own reported failure as success"
    );
}

/// THE DISCRIMINATING CONTROL. A run where every PR is created must still
/// return Ok. Without this case, "return Err whenever anything was attempted"
/// and "return Err only when something failed" are indistinguishable, and the
/// witness above would be satisfied by a command that always fails.
#[tokio::test]
async fn a_run_with_no_failures_still_exits_zero() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("frontend").build();
    let mut manifest = ws.load_manifest();
    manifest.settings.target = Some("dev".to_string());

    repo_ahead_of(&ws, "frontend", "dev", "feat/thing");
    point_repo_at_mock(&mut manifest, "frontend", &server);

    mock_branch_exists(&server, "owner", "repo", "dev").await;
    mock_create_pr(&server, 11, "https://github.com/owner/repo/pull/11").await;

    let filter = vec!["frontend".to_string()];
    let result = gitgrip::cli::commands::pr::run_pr_create(
        &ws.workspace_root,
        &manifest,
        Some("Add feature"),
        None,
        false,
        false,
        false,
        Some(&filter),
        None,
        false,
    )
    .await;

    let requests = server.received_requests().await.unwrap();
    let posts = requests
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/pulls"))
        .count();
    assert_eq!(posts, 1, "control: exactly one PR should have been created");

    assert!(
        result.is_ok(),
        "a run in which every PR was created must not report failure: {result:?}"
    );
}

/// `--json` keeps exit 0 and carries the failure in the body.
///
/// Not an exception carved out for convenience -- it is this repo's shipped
/// convention, and it is shipped rather than merely planned: `gr verify --json`
/// returns Ok before its own `exit(1)` (verify.rs:101), and docs/PLAN-verify.md
/// gives the reason. A caller who asked for JSON is parsing the body by
/// construction, and a non-zero exit makes a `set -e` script die before it can
/// read the answer it asked for.
///
/// Without this case, the fix above would silently change the exit status of
/// every scripted `--json` caller, which is the sort of thing that gets found
/// in someone else's CI rather than here.
#[tokio::test]
async fn json_mode_reports_the_failure_in_the_body_and_still_exits_zero() {
    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("frontend").build();
    let mut manifest = ws.load_manifest();
    manifest.settings.target = Some("dev".to_string());

    repo_ahead_of(&ws, "frontend", "dev", "feat/thing");
    point_repo_at_mock(&mut manifest, "frontend", &server);

    mock_branch_exists(&server, "owner", "repo", "dev").await;
    mock_create_pr_validation_error(&server).await;

    let filter = vec!["frontend".to_string()];
    let result = gitgrip::cli::commands::pr::run_pr_create(
        &ws.workspace_root,
        &manifest,
        Some("Add feature"),
        None,
        false,
        false,
        false,
        Some(&filter),
        None,
        true, // json
    )
    .await;

    // Control: the same inputs in human mode DO error. Without this the test
    // would pass equally well against a build where nothing ever errors, which
    // is the state this whole PR exists to leave behind.
    let requests = server.received_requests().await.unwrap();
    let posts = requests
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/pulls"))
        .count();
    assert!(posts >= 1, "control: the creation must have been attempted");

    assert!(
        result.is_ok(),
        "--json must keep exit 0 and carry pass/fail in the body: {result:?}"
    );
}

/// THE WIRING WITNESS: the bytes the shipped binary actually prints.
///
/// Sentinel blocked v2 on exactly this gap, and proved it rather than argued
/// it: he left `pr_create_json_payload` correct and set `result.success = true`
/// inside `run_pr_create` immediately before the production
/// `to_string_pretty` call. All six existing witnesses stayed green. A
/// scripted caller would then have received process success AND payload
/// success after the platform rejected the creation -- both instruments
/// agreeing, both wrong, which is the whole defect this PR exists to close.
///
/// The reason the helper tests could not see it is that they call
/// `pr_create_json_payload` and `to_string_pretty` themselves. That pins
/// CONSTRUCTION; it says nothing about whether the production caller emits
/// those bytes unchanged. This is my own filed lesson -- a witness calling a
/// helper directly pins the helper, not its use -- committed again one layer
/// out from where I fixed it in v1.
///
/// So this test spawns the REAL `gr` binary, in a real workspace, against the
/// mock platform, and parses the JSON off its stdout. Nothing between the
/// construction site and the process's output can hide from it: the assertion
/// is on the bytes a caller parses, because that is the only surface the
/// exit-0 convention is defensible on.
#[tokio::test]
async fn the_shipped_binary_prints_success_false_after_a_platform_failure() {
    use assert_cmd::Command as AssertCommand;

    let (server, _adapter) = setup_github_mock().await;

    let ws = WorkspaceBuilder::new().add_repo("frontend").build();
    repo_ahead_of(&ws, "frontend", "dev", "feat/thing");

    mock_branch_exists(&server, "owner", "repo", "dev").await;
    mock_create_pr_validation_error(&server).await;

    // The subprocess reads the manifest from disk, so the mock repoint that
    // `point_repo_at_mock` does in memory has to be persisted here instead.
    let manifest_path = ws
        .workspace_root
        .join(".gitgrip")
        .join("spaces")
        .join("main")
        .join("gripspace.yml");
    let mut manifest: serde_yaml::Value =
        serde_yaml::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();
    let repo = manifest
        .get_mut("repos")
        .and_then(|r| r.get_mut("frontend"))
        .expect("fixture must define the frontend repo");
    repo["url"] = serde_yaml::Value::String("https://github.com/owner/repo.git".into());
    repo["platform"] =
        serde_yaml::from_str(&format!("type: github\nbase_url: {}\n", server.uri())).unwrap();
    manifest["settings"]["target"] = serde_yaml::Value::String("dev".into());
    std::fs::write(&manifest_path, serde_yaml::to_string(&manifest).unwrap()).unwrap();

    let out = AssertCommand::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .env("GITHUB_TOKEN", "mock-test-token")
        .args([
            "pr",
            "create",
            "-t",
            "Add feature",
            "--repo",
            "frontend",
            "--json",
        ])
        .output()
        .unwrap();

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();

    // HARNESS GUARD BEFORE ANY CONTENT CLAIM, and it is also the exit-0 half of
    // the `--json` convention -- one assertion serving both, asserted on this
    // run rather than inferred from a different one. It has to come FIRST:
    // `cargo test --test <name>` does not reliably rebuild the bin `cargo_bin`
    // invokes, and a stale or half-written binary yields empty stdout whose
    // failure reads exactly like a missing feature. Absent and could-not-look
    // must not share a failure.
    //
    // It sat LAST until the v4 gate, where Atlas caught that this file's body
    // claimed both subprocess witnesses guarded exit 0 first and only witness
    // (5) did. Same shape as the three rounds above: a universal asserted over
    // two things after checking one.
    assert_eq!(
        out.status.code(),
        Some(0),
        "harness/contract: `--json` keeps exit 0 and carries pass/fail in the \
         body ({:?}); stdout={stdout} stderr={}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );

    let parsed: serde_json::Value = serde_json::from_str(stdout.trim()).unwrap_or_else(|e| {
        panic!(
            "harness: --json must print parseable JSON on stdout ({e}); got: \
             {stdout} stderr={}",
            String::from_utf8_lossy(&out.stderr)
        )
    });

    // Control: the run must have REACHED the platform. Without this, a binary
    // that failed to load the workspace at all would print no JSON and the
    // absence assertions below would pass for the wrong reason -- the same
    // never-ran-but-looks-right shape the helper tests fell into.
    let posts = server
        .received_requests()
        .await
        .unwrap()
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/pulls"))
        .count();
    assert!(
        posts >= 1,
        "control: the binary must have attempted the creation. stdout={stdout} stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );

    assert_eq!(
        parsed["success"],
        serde_json::Value::Bool(false),
        "the shipped binary reported success after the platform refused: {stdout}"
    );
    assert!(
        stdout.contains("frontend"),
        "the payload must name the repo that failed: {stdout}"
    );
}

/// THE SECOND WIRE: the empty-`branch_groups` early return.
///
/// Sentinel and Atlas found this independently at the v3 gate. `run_pr_create`
/// had TWO JSON serialization sites, and they disagreed about identical
/// inputs: the terminal branch computed `success: false` from
/// zero-created/zero-failed, while this early return hardcoded
/// `success: true`. Both were already shipping, so which answer a consumer got
/// depended on how far the command happened to get.
///
/// The helper unit for the no-op case could not see it, because it calls
/// `pr_create_json_payload` directly — the same helper-not-use shape this file
/// names twice already. Only the shipped binary can tell these two routes
/// apart, which is why this witness is a subprocess one.
///
/// Note what this pins and what it does not: it pins that BOTH routes now come
/// through one construction site, which is what makes the "built in one
/// function" claim checkable rather than asserted. It is deliberately not a
/// ruling on whether a no-op *ought* to read `success: false` — that is the
/// separate zero-match question, and the point here is that production must
/// not answer it two different ways at once.
#[tokio::test]
async fn the_early_no_op_return_serializes_through_the_same_helper() {
    use assert_cmd::Command as AssertCommand;

    let ws = WorkspaceBuilder::new().add_repo("frontend").build();
    // No branch, no commits: nothing is ahead, so `branch_groups` is empty and
    // production takes the early return rather than the terminal branch.

    let out = AssertCommand::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .env("GITHUB_TOKEN", "mock-test-token")
        .args(["pr", "create", "-t", "Nothing to do", "--json"])
        .output()
        .unwrap();

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();

    // HARNESS GUARD BEFORE ANY CONTENT CLAIM. `cargo test --test <name>` does
    // not reliably rebuild the bin `cargo_bin` invokes, and a stale or
    // half-written binary yields empty stdout whose failure reads exactly like
    // a missing feature. Absent and could-not-look must not share a failure.
    assert!(
        out.status.success(),
        "harness: `gr pr create --json` did not exit 0 ({:?}); stderr={}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );
    let parsed: serde_json::Value = serde_json::from_str(stdout.trim()).unwrap_or_else(|e| {
        panic!(
            "harness: --json must print parseable JSON on stdout ({e}); got: {stdout} stderr={}",
            String::from_utf8_lossy(&out.stderr)
        )
    });

    // ROUTE CONTROL, and it has to be a discriminating one. Empty `prs` and
    // `failed` do NOT identify the early return -- the terminal branch reports
    // the same arrays for a run that built groups and created nothing. So the
    // control is the one string only the early return can print: the same
    // workspace in human mode must say it. Without this the witness could pass
    // while silently covering the wrong wire, which is the failure this whole
    // PR keeps rediscovering.
    let human = AssertCommand::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .env("GITHUB_TOKEN", "mock-test-token")
        .args(["pr", "create", "-t", "Nothing to do"])
        .output()
        .unwrap();
    let human_stdout = String::from_utf8_lossy(&human.stdout).to_string();
    assert!(
        human_stdout.contains("No repositories have changes to create PRs for"),
        "control: this witness must exercise the empty-branch_groups early \
         return, and only that route prints this line. Got: {human_stdout} \
         stderr={}",
        String::from_utf8_lossy(&human.stderr)
    );

    assert_eq!(
        parsed["prs"].as_array().map(|a| a.len()),
        Some(0),
        "the zero-group route reports nothing created: {stdout}"
    );
    assert_eq!(
        parsed["failed"].as_array().map(|a| a.len()),
        Some(0),
        "the zero-group route reports nothing failed: {stdout}"
    );

    assert_eq!(
        parsed["success"],
        serde_json::Value::Bool(false),
        "the early return must serialize through pr_create_json_payload, which \
         computes success from the inputs, rather than hardcoding a different \
         answer than the terminal branch gives for the same state: {stdout}"
    );
}

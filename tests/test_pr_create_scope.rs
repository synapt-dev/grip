//! `gr pr create` opens PRs for the branch the user means, not for every
//! branch some repo happens to sit on.
//!
//! The command groups repos by their CURRENT branch and used to open a PR for
//! every group, each under the one `-t` title. A manifest repository parked
//! on an old WIP branch with one commit ahead of main therefore opened a
//! stray PR at every `gr pr create`, titled after an unrelated feature
//! (observed twice, weeks apart, on the same WIP branch). These tests pin the
//! scope: the invoking repo's branch opens, the rest are listed, and a run
//! from outside any repo with disagreeing groups refuses.

mod common;

use std::path::Path;

use common::fixtures::{WorkspaceBuilder, WorkspaceFixture};
use common::git_helpers;
use common::mock_platform::{
    mock_branch_exists, mock_create_pr, point_repo_at_mock, setup_github_mock,
};
use wiremock::http::Method;
use wiremock::MockServer;

/// Put `repo` on `branch` with one commit ahead of `origin/main`.
fn ahead_on(path: &Path, branch: &str) {
    git_helpers::create_branch(path, branch);
    git_helpers::commit_file(
        path,
        &format!("{}.txt", branch.replace('/', "-")),
        "x",
        "ahead",
    );
}

/// Give the manifest repo an origin with `main`, as a real gripspace has.
fn manifest_with_origin(ws: &WorkspaceFixture) -> std::path::PathBuf {
    let dir = ws
        .workspace_root
        .join(".gitgrip")
        .join("spaces")
        .join("main");
    let bare = ws.remotes_dir.join("manifest.git");
    git_helpers::init_bare_repo(&bare);
    git_helpers::add_remote(&dir, "origin", &format!("file://{}", bare.display()));
    git_helpers::push_branch(&dir, "origin", "main");
    dir
}

fn point_manifest_at_mock(manifest: &mut gitgrip::core::manifest::Manifest, server: &MockServer) {
    let m = manifest
        .manifest
        .as_mut()
        .expect("fixture has a manifest repo");
    m.url = "https://github.com/owner/repo.git".to_string();
    m.platform = Some(gitgrip::core::manifest::PlatformConfig {
        platform_type: gitgrip::core::manifest::PlatformType::GitHub,
        base_url: Some(server.uri()),
    });
}

/// The `head` of every PR the command POSTed, in order.
async fn opened_heads(server: &MockServer) -> Vec<String> {
    server
        .received_requests()
        .await
        .unwrap()
        .iter()
        .filter(|r| r.method == Method::POST && r.url.path().ends_with("/pulls"))
        .map(|r| {
            let v: serde_json::Value = serde_json::from_slice(&r.body).unwrap();
            v["head"].as_str().unwrap_or("").to_string()
        })
        .collect()
}

async fn run(
    ws: &WorkspaceFixture,
    manifest: &gitgrip::core::manifest::Manifest,
    filter: Option<Vec<String>>,
    from: &Path,
) -> anyhow::Result<()> {
    gitgrip::cli::commands::pr::run_pr_create_in(
        &ws.workspace_root,
        manifest,
        Some("docs: an unrelated feature"),
        None,
        false,
        false,
        false,
        filter.as_deref(),
        None,
        false,
        Some(from),
    )
    .await
}

async fn mocked() -> MockServer {
    let (server, _adapter) = setup_github_mock().await;
    mock_branch_exists(&server, "owner", "repo", "main").await;
    mock_create_pr(&server, 1, "https://github.com/owner/repo/pull/1").await;
    server
}

/// Row 1, THE OBSERVED SHAPE: the feature is in `frontend`, the manifest sits
/// on an old WIP branch with a real commit. Run from `frontend`, exactly one
/// PR opens, for the feature branch.
#[tokio::test]
async fn a_manifest_on_another_branch_does_not_get_a_pr() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .with_manifest_repo()
        .build();
    let mdir = manifest_with_origin(&ws);
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_manifest_at_mock(&mut manifest, &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    ahead_on(&mdir, "wip/pre-restart-snapshot");

    let r = run(&ws, &manifest, None, &ws.repo_path("frontend")).await;
    assert!(r.is_ok(), "command errored: {r:?}");
    assert_eq!(opened_heads(&server).await, vec!["feat/x".to_string()]);
}

/// Row 1b, the same class with an ordinary repo: `backend` parked on some
/// other branch with a commit ahead must not get the feature's title either.
#[tokio::test]
async fn an_ordinary_repo_on_another_branch_does_not_get_a_pr() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_repo_at_mock(&mut manifest, "backend", &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    ahead_on(&ws.repo_path("backend"), "wip/other");

    let r = run(&ws, &manifest, None, &ws.repo_path("frontend")).await;
    assert!(r.is_ok(), "command errored: {r:?}");
    assert_eq!(opened_heads(&server).await, vec!["feat/x".to_string()]);
}

/// Row 2, control: when the manifest is ON the feature branch with a commit,
/// it is in scope like any other repo. Without this row, "never open the
/// manifest" would pass row 1.
#[tokio::test]
async fn a_manifest_on_the_same_branch_is_in_scope() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .with_manifest_repo()
        .build();
    let mdir = manifest_with_origin(&ws);
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_manifest_at_mock(&mut manifest, &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    ahead_on(&mdir, "feat/x");

    let r = run(&ws, &manifest, None, &ws.repo_path("frontend")).await;
    assert!(r.is_ok(), "command errored: {r:?}");
    assert_eq!(
        opened_heads(&server).await,
        vec!["feat/x".to_string(), "feat/x".to_string()],
        "both frontend and the manifest are on feat/x with commits ahead"
    );
}

/// Row 3: a manifest on the feature branch with UNCOMMITTED changes and no
/// commit of its own is not PR content, even when named explicitly.
#[tokio::test]
async fn a_dirty_manifest_with_no_commits_is_not_opened() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .with_manifest_repo()
        .build();
    let mdir = manifest_with_origin(&ws);
    let mut manifest = ws.load_manifest();
    point_manifest_at_mock(&mut manifest, &server);

    git_helpers::create_branch(&mdir, "feat/x");
    std::fs::write(mdir.join("scratch.txt"), "uncommitted").unwrap();

    let r = run(&ws, &manifest, Some(vec!["manifest".to_string()]), &mdir).await;
    assert!(r.is_ok(), "command errored: {r:?}");
    assert!(
        opened_heads(&server).await.is_empty(),
        "uncommitted changes alone must not open a PR"
    );
}

/// Row 4: run from the gripspace root (inside no repo) while the groups
/// disagree -- there is no branch to scope to, so the command refuses and
/// opens nothing.
#[tokio::test]
async fn from_the_root_with_two_branches_it_refuses() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_repo_at_mock(&mut manifest, "backend", &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    ahead_on(&ws.repo_path("backend"), "wip/other");

    let r = run(&ws, &manifest, None, &ws.workspace_root).await;
    let err = r.expect_err("two branches from the root must refuse");
    let msg = err.to_string();
    assert!(msg.contains("refusing"), "message: {msg}");
    assert!(
        msg.contains("feat/x") && msg.contains("wip/other"),
        "names both groups: {msg}"
    );
    assert!(opened_heads(&server).await.is_empty());
}

/// Row 5: run from a repo that is ON its target with nothing to open, while
/// another repo has commits ahead on its own branch. Nothing opens, and it is
/// not an error: the other group is listed, not refused.
#[tokio::test]
async fn from_a_repo_on_its_target_nothing_opens_and_it_is_not_an_error() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_repo_at_mock(&mut manifest, "backend", &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    // backend stays on main, its target

    let r = run(&ws, &manifest, None, &ws.repo_path("backend")).await;
    assert!(r.is_ok(), "nothing-to-do must not be an error: {r:?}");
    assert!(opened_heads(&server).await.is_empty());
}

/// Row 6, control for row 4: from the root with ONE branch group there is
/// nothing ambiguous, and the group opens (the common multi-repo feature flow).
#[tokio::test]
async fn from_the_root_with_one_branch_it_opens() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_repo_at_mock(&mut manifest, "backend", &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    ahead_on(&ws.repo_path("backend"), "feat/x");

    let r = run(&ws, &manifest, None, &ws.workspace_root).await;
    assert!(r.is_ok(), "command errored: {r:?}");
    assert_eq!(opened_heads(&server).await.len(), 2);
}

/// Row 7, the escape hatch the skip line names: repos named with --repo open
/// even when they are on different branches.
#[tokio::test]
async fn named_repos_open_even_across_branches() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let mut manifest = ws.load_manifest();
    point_repo_at_mock(&mut manifest, "frontend", &server);
    point_repo_at_mock(&mut manifest, "backend", &server);

    ahead_on(&ws.repo_path("frontend"), "feat/x");
    ahead_on(&ws.repo_path("backend"), "wip/other");

    let filter = vec!["frontend".to_string(), "backend".to_string()];
    let r = run(&ws, &manifest, Some(filter), &ws.repo_path("frontend")).await;
    assert!(r.is_ok(), "command errored: {r:?}");
    let mut heads = opened_heads(&server).await;
    heads.sort();
    assert_eq!(heads, vec!["feat/x".to_string(), "wip/other".to_string()]);
}

/// Row 8: the skip line's own advice works. `--repo manifest` alone selects
/// the manifest on its branch. Before this change the filter validation
/// rejected the name "manifest" outright, so the advice would have errored.
#[tokio::test]
async fn repo_manifest_selects_the_manifest() {
    let server = mocked().await;
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .with_manifest_repo()
        .build();
    let mdir = manifest_with_origin(&ws);
    let mut manifest = ws.load_manifest();
    point_manifest_at_mock(&mut manifest, &server);

    ahead_on(&mdir, "chore/manifest-change");

    let r = run(
        &ws,
        &manifest,
        Some(vec!["manifest".to_string()]),
        &ws.repo_path("frontend"),
    )
    .await;
    assert!(r.is_ok(), "command errored: {r:?}");
    assert_eq!(
        opened_heads(&server).await,
        vec!["chore/manifest-change".to_string()]
    );
}

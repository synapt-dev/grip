//! Integration tests for the push command.

mod common;

use common::assertions::assert_on_branch;
use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_push_to_remote() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create branch, make changes, commit
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/push-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("app").join("pushed.txt"), "content").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: push test",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Push with set-upstream
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true, // set_upstream
        false,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "push should succeed: {:?}", result.err());

    // Verify the branch exists on the remote
    assert!(
        git_helpers::branch_exists(&ws.repo_path("app"), "feat/push-test"),
        "branch should exist locally"
    );
}

#[test]
fn test_push_nothing_to_push() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Push with nothing to push -- should succeed
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "push with nothing should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_push_skips_reference_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("app")
        .add_reference_repo("docs")
        .build();

    let manifest = ws.load_manifest();

    // Create branch in app only (reference repos are skipped)
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/ref-test"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("app").join("change.txt"), "data").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "change",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Push -- should skip reference repo
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "push should succeed: {:?}", result.err());

    // docs should still be on main (not pushed, not branched)
    assert_on_branch(&ws.repo_path("docs"), "main");
}

#[test]
fn test_push_multiple_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Create branch, commit in both
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/multi-push"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("frontend").join("fe.txt"), "fe").unwrap();
    std::fs::write(ws.repo_path("backend").join("be.txt"), "be").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: multi push",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    );
    assert!(result.is_ok(), "push should succeed: {:?}", result.err());
}

#[test]
fn test_push_force() {
    let ws = WorkspaceBuilder::new().add_repo("app").build();

    let manifest = ws.load_manifest();

    // Create branch, commit, push
    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/force-push"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    std::fs::write(ws.repo_path("app").join("first.txt"), "first").unwrap();
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "first commit",
        false,
        false,
        None,
        None,
    )
    .unwrap();
    gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Make another commit
    std::fs::write(ws.repo_path("app").join("second.txt"), "second").unwrap();
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "second commit",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Force push
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        false,
        true, // force
        false,
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "force push should succeed: {:?}",
        result.err()
    );
}

#[test]
fn test_push_quiet_mode() {
    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();

    let manifest = ws.load_manifest();

    // Quiet push with nothing to push should succeed (suppresses "nothing to push" messages)
    let result = gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        false,
        false,
        true, // quiet
        false,
        None,
        None,
    );
    assert!(
        result.is_ok(),
        "quiet push should succeed: {:?}",
        result.err()
    );
}

/// The summary must NAME what it pushed, not just count it (#921).
///
/// `gr push` writes to every remote in the workspace that is ahead, and the
/// old summary said only "Pushed 2 repo(s), 15 had nothing to push." The
/// per-repo lines above it named the repos with NOTHING to push, so an
/// operator could see everything that did not happen and nothing that did.
///
/// That is a reporting defect with a real consequence: a workspace can mix
/// public and private remotes, so someone publishing one reviewed branch also
/// publishes anything else that happens to be ahead, and the summary gave them
/// no way to notice.
///
/// Asserted against the SHIPPED BINARY's stdout rather than by calling
/// `run_push` in-process, because the summary is a `println!` and the thing
/// under test is what an operator reads. An in-process call cannot see it.
#[test]
fn the_push_summary_names_each_repo_it_pushed_and_only_those() {
    use assert_cmd::Command as AssertCommand;

    let ws = WorkspaceBuilder::new()
        .add_repo("frontend")
        .add_repo("backend")
        .build();
    let manifest = ws.load_manifest();

    gitgrip::cli::commands::branch::run_branch(gitgrip::cli::commands::branch::BranchOptions {
        workspace_root: &ws.workspace_root,
        manifest: &manifest,
        name: Some("feat/named-summary"),
        delete: false,
        move_commits: false,
        repos_filter: None,
        group_filter: None,
        include_parked: false,
        json: false,
    })
    .unwrap();

    // Land both remotes first, so the second push has one repo ahead and one
    // genuinely up to date. That asymmetry is the whole point of the test.
    for repo in ["frontend", "backend"] {
        std::fs::write(ws.repo_path(repo).join("seed.txt"), "seed").unwrap();
    }
    let files = vec![".".to_string()];
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "chore: seed",
        false,
        false,
        None,
        None,
    )
    .unwrap();
    gitgrip::cli::commands::push::run_push(
        &ws.workspace_root,
        &manifest,
        true,
        false,
        false,
        false,
        None,
        None,
    )
    .unwrap();

    // Now only `frontend` is ahead.
    std::fs::write(ws.repo_path("frontend").join("fe.txt"), "fe").unwrap();
    gitgrip::cli::commands::add::run_add(&ws.workspace_root, &manifest, &files, None, None)
        .unwrap();
    gitgrip::cli::commands::commit::run_commit(
        &ws.workspace_root,
        &manifest,
        "feat: only frontend",
        false,
        false,
        None,
        None,
    )
    .unwrap();

    let out = AssertCommand::cargo_bin("gr1")
        .unwrap()
        .current_dir(&ws.workspace_root)
        .args(["push"])
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();

    // HARNESS GUARD, BEFORE ANY CONTENT CLAIM. A subprocess witness can be
    // handed a binary that is stale, half-written, or never rebuilt --
    // `cargo test --test <name>` does not reliably rebuild the bin that
    // `cargo_bin` invokes, and one run of this test produced EMPTY stdout for
    // exactly that reason while its failure message read like the feature was
    // missing. Absent and could-not-look must not share a failure.
    assert!(
        out.status.success(),
        "harness: `gr push` did not exit 0 ({:?}); stderr={}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        stdout.contains("Pushed"),
        "harness: the binary produced no push summary at all, so this run says \
         nothing about naming. stdout={stdout} stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );

    // Read the sha from the BARE REMOTE, not from the local worktree.
    // The production code prints the local HEAD after the push returns Ok, so a
    // local read would have the test and the subject consulting the SAME object
    // and the assertion below would hold even if nothing ever reached a remote.
    // The claim is "the commit the remote now carries", so the remote is what
    // the witness must read.
    let remote_head = String::from_utf8(
        std::process::Command::new("git")
            .args([
                "-C",
                ws.remote_path("frontend").to_str().unwrap(),
                "rev-parse",
                // --verify, so an unknown ref EXITS NON-ZERO with empty stdout.
                // Without it rev-parse echoes the ref name back, and
                // "refs/heads/" plus a 29-character branch is exactly 40
                // characters -- the length check below would pass on the error
                // string itself.
                "--verify",
                "refs/heads/feat/named-summary^{commit}",
            ])
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_string();
    assert!(
        remote_head.len() == 40 && remote_head.chars().all(|c| c.is_ascii_hexdigit()),
        "harness: could not read the pushed ref from the bare remote; got {remote_head:?}"
    );
    let head = remote_head;

    assert!(
        stdout.contains("frontend"),
        "the summary must name the repo it pushed: {stdout}"
    );
    assert!(
        stdout.contains("refs/heads/feat/named-summary"),
        "the summary must name the ref it pushed: {stdout}"
    );
    assert!(
        stdout.contains(&head[..12]),
        "the summary must name the commit the remote now carries ({}): {stdout}",
        &head[..12]
    );

    // THE DISCRIMINATING CONTROL. Without it, an implementation that simply
    // printed every repo in the workspace would satisfy every assertion above
    // -- and that implementation is worse than the count it replaced, because
    // it would name repos it never pushed.
    let pushed_block: String = stdout
        .lines()
        .filter(|l| l.contains("refs/heads/"))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !pushed_block.contains("backend"),
        "a repo with nothing to push must not be listed as pushed: {stdout}"
    );
    assert!(
        stdout.contains("had nothing to push"),
        "the up-to-date repo must still be accounted for in the summary: {stdout}"
    );
}

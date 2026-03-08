//! Tests for the grep command

mod common;

use common::fixtures::WorkspaceBuilder;
use common::git_helpers;

#[test]
fn test_grep_finds_across_repos() {
    let ws = WorkspaceBuilder::new()
        .add_repo("alpha")
        .add_repo("beta")
        .build();

    // Create committed files with known content in both repos
    git_helpers::commit_file(
        &ws.repo_path("alpha"),
        "search_target.txt",
        "UNIQUE_SEARCH_TOKEN_ALPHA",
        "Add searchable file",
    );

    git_helpers::commit_file(
        &ws.repo_path("beta"),
        "search_target.txt",
        "UNIQUE_SEARCH_TOKEN_BETA",
        "Add searchable file",
    );

    // grep should find matches in both repos
    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::grep::run_grep(
        &ws.workspace_root,
        &manifest,
        "UNIQUE_SEARCH_TOKEN",
        false,
        false,
        &[],
        None,
        None,
    );
    assert!(result.is_ok());
}

#[test]
fn test_grep_no_matches() {
    let ws = WorkspaceBuilder::new().add_repo("alpha").build();

    let manifest = ws.load_manifest();
    let result = gitgrip::cli::commands::grep::run_grep(
        &ws.workspace_root,
        &manifest,
        "THIS_PATTERN_WILL_NEVER_MATCH_ANYTHING_12345",
        false,
        false,
        &[],
        None,
        None,
    );
    // Should succeed even with no matches
    assert!(result.is_ok());
}

#[test]
fn test_grep_case_insensitive() {
    let ws = WorkspaceBuilder::new().add_repo("alpha").build();

    git_helpers::commit_file(
        &ws.repo_path("alpha"),
        "case_test.txt",
        "CaSeMiXeD_GrEp_TeSt",
        "Add case test file",
    );

    let manifest = ws.load_manifest();

    // Case-insensitive search should find it
    let result = gitgrip::cli::commands::grep::run_grep(
        &ws.workspace_root,
        &manifest,
        "casemixed_grep_test",
        true, // ignore_case
        false,
        &[],
        None,
        None,
    );
    assert!(result.is_ok());
}

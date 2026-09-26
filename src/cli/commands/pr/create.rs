//! PR create command implementation

use std::collections::BTreeMap;
use std::path::Path;

use git2::Repository;

use crate::cli::output::Output;
use crate::core::manifest::{Manifest, PlatformType};
use crate::core::repo::{filter_repos, get_manifest_repo_info, RepoInfo};
use crate::core::state::StateFile;
use crate::git::remote::{get_remote_url, set_remote_url};
use crate::git::{get_current_branch, open_repo, path_exists};
use crate::platform::get_platform_adapter;
use tracing::debug;

/// A group of repos all on the same feature branch
struct BranchGroup {
    branch: String,
    repos: Vec<RepoInfo>,
}

/// What `gr pr create` opens, decided before anything touches a remote.
#[derive(Debug)]
pub(crate) enum PrScope {
    /// Open these branch groups; `skipped` lists (repo, branch) left out.
    Open {
        open: BTreeMap<String, Vec<RepoInfo>>,
        skipped: Vec<(String, String)>,
    },
    /// Run from outside any repo while the groups disagree.
    Refuse { groups: Vec<(String, Vec<String>)> },
}

/// Scope branch groups to the branch the user means.
///
/// - `explicit_filter`: the user named repos with --repo, so every group
///   among them opens (that is the escape hatch the skip line points at).
/// - `invoked_branch = Some(b)`: run from inside a repo on branch `b`; only
///   group `b` opens and the rest are skipped. If there is no group `b` (the
///   invoking repo is on its target, or has nothing ahead) nothing opens.
/// - `invoked_branch = None` (run from the gripspace root): one group opens;
///   two or more refuse.
pub(crate) fn scope_branch_groups(
    groups: BTreeMap<String, Vec<RepoInfo>>,
    invoked_branch: Option<&str>,
    explicit_filter: bool,
) -> PrScope {
    if explicit_filter {
        return PrScope::Open {
            open: groups,
            skipped: Vec::new(),
        };
    }
    match invoked_branch {
        Some(b) => {
            let mut open = BTreeMap::new();
            let mut skipped = Vec::new();
            for (branch, repos) in groups {
                if branch == b {
                    open.insert(branch, repos);
                } else {
                    skipped.extend(repos.into_iter().map(|r| (r.name, branch.clone())));
                }
            }
            PrScope::Open { open, skipped }
        }
        None if groups.len() > 1 => PrScope::Refuse {
            groups: groups
                .into_iter()
                .map(|(b, rs)| (b, rs.into_iter().map(|r| r.name).collect()))
                .collect(),
        },
        None => PrScope::Open {
            open: groups,
            skipped: Vec::new(),
        },
    }
}

/// The current branch of the repo containing `cwd` (the deepest match), or
/// None when `cwd` is inside none of `candidates`.
fn invoking_repo_branch(cwd: &Path, candidates: &[RepoInfo]) -> Option<String> {
    let cwd = cwd.canonicalize().ok()?;
    let repo = candidates
        .iter()
        .filter_map(|r| r.absolute_path.canonicalize().ok().map(|p| (p, r)))
        .filter(|(p, _)| cwd.starts_with(p))
        .max_by_key(|(p, _)| p.components().count())?
        .1;
    let git_repo = open_repo(&repo.absolute_path).ok()?;
    get_current_branch(&git_repo).ok()
}

/// Convert a branch name to a PR title
fn branch_to_title(branch: &str) -> String {
    let title = branch
        .trim_start_matches("feat/")
        .trim_start_matches("fix/")
        .trim_start_matches("chore/")
        .replace(['-', '_'], " ");
    let mut chars = title.chars();
    match chars.next() {
        None => title,
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

#[derive(serde::Serialize)]
pub(crate) struct JsonPrCreateResult {
    success: bool,
    prs: Vec<JsonCreatedPr>,
    failed: Vec<JsonFailedRepo>,
}

#[derive(serde::Serialize)]
struct JsonCreatedPr {
    repo: String,
    branch: String,
    number: u64,
    url: String,
}

#[derive(serde::Serialize)]
struct JsonFailedRepo {
    repo: String,
    reason: String,
}

/// The `--json` payload, built in ONE place so it can be witnessed.
///
/// `--json` deliberately keeps exit 0 and carries pass/fail in the body — the
/// shipped convention, `gr verify --json` returns `Ok` before its own
/// `exit(1)`. That trade is only safe while the payload is TRUE, so `success`
/// is the load-bearing field of the whole design call, and it was previously
/// computed inline inside `run_pr_create` where no test could reach it.
///
/// Both gate reviewers proved the consequence rather than arguing it: they
/// mutated `success` to unconditional `true` and every test still passed. A
/// scripted caller would then have received process success AND payload
/// success after the platform rejected the creation — both instruments
/// agreeing, both wrong. Extracting the construction gives that field a
/// witness.
///
/// That witness is necessary and was never sufficient, which took two more
/// gate rounds to establish. A unit calling this function pins CONSTRUCTION
/// and says nothing about whether production emits the bytes it builds, so a
/// call-site overwrite survived it. And "the only construction site" is a
/// claim about EVERY production return: `run_pr_create` had a second,
/// inline one at the empty-`branch_groups` early return that hardcoded
/// `success: true` for the same zero/zero state this computes `false` from.
/// Both routes now come through here, and both are witnessed against the
/// shipped binary's stdout rather than against this function.
pub(crate) fn pr_create_json_payload(
    created: &[(String, String, u64, String)],
    failed: &[(String, String)],
) -> JsonPrCreateResult {
    JsonPrCreateResult {
        success: !created.is_empty() && failed.is_empty(),
        prs: created
            .iter()
            .map(|(branch, repo, number, url)| JsonCreatedPr {
                repo: repo.clone(),
                branch: branch.clone(),
                number: *number,
                url: url.clone(),
            })
            .collect(),
        failed: failed
            .iter()
            .map(|(repo, reason)| JsonFailedRepo {
                repo: repo.clone(),
                reason: reason.clone(),
            })
            .collect(),
    }
}

/// Run the PR create command
#[allow(clippy::too_many_arguments)]
pub async fn run_pr_create(
    workspace_root: &Path,
    manifest: &Manifest,
    title: Option<&str>,
    body: Option<&str>,
    draft: bool,
    push_first: bool,
    dry_run: bool,
    repo_filter: Option<&[String]>,
    base_override: Option<&str>,
    json: bool,
) -> anyhow::Result<()> {
    let cwd = std::env::current_dir().ok();
    run_pr_create_in(
        workspace_root,
        manifest,
        title,
        body,
        draft,
        push_first,
        dry_run,
        repo_filter,
        base_override,
        json,
        cwd.as_deref(),
    )
    .await
}

/// `run_pr_create` with the invoking directory passed in rather than read
/// from the process, so a test can say where the command was run from.
#[allow(clippy::too_many_arguments)]
pub async fn run_pr_create_in(
    workspace_root: &Path,
    manifest: &Manifest,
    title: Option<&str>,
    body: Option<&str>,
    draft: bool,
    push_first: bool,
    dry_run: bool,
    repo_filter: Option<&[String]>,
    base_override: Option<&str>,
    json: bool,
    invoked_from: Option<&Path>,
) -> anyhow::Result<()> {
    if !json {
        if dry_run {
            Output::header("PR Preview");
            println!();
        } else {
            Output::header("Creating pull requests...");
            println!();
        }
    }

    let repos = filter_repos(manifest, workspace_root, repo_filter, None, false);

    // Validate repo filter
    if let Some(filter) = repo_filter {
        let repo_names: Vec<&str> = repos.iter().map(|r| r.name.as_str()).collect();
        // "manifest" names the manifest repo, which filter_repos never lists.
        // Rejecting it here made the include_manifest branch below dead code,
        // so `--repo manifest` could never select the manifest.
        let has_manifest_repo = get_manifest_repo_info(manifest, workspace_root).is_some();
        for name in filter {
            if name == "manifest" && has_manifest_repo {
                continue;
            }
            if !repo_names.contains(&name.as_str()) {
                anyhow::bail!("Repository '{}' not found in manifest", name);
            }
        }
    }

    // Group repos by their current feature branch
    let mut branch_groups: BTreeMap<String, Vec<RepoInfo>> = BTreeMap::new();

    for repo in &repos {
        if !path_exists(&repo.absolute_path) {
            continue;
        }

        match open_repo(&repo.absolute_path) {
            Ok(git_repo) => {
                let current = match get_current_branch(&git_repo) {
                    Ok(b) => b,
                    Err(_) => continue,
                };

                let target = base_override.unwrap_or_else(|| repo.target_branch());

                // Skip if on target branch
                if current == target {
                    continue;
                }

                // Check for changes ahead of target branch
                if has_commits_ahead(&git_repo, &current, target)? {
                    branch_groups.entry(current).or_default().push(repo.clone());
                }
            }
            Err(e) => Output::error(&format!("{}: {}", repo.name, e)),
        }
    }

    // Also check the manifest repo for changes (if not excluded by repo filter)
    let include_manifest = repo_filter
        .map(|f| f.iter().any(|n| n == "manifest"))
        .unwrap_or(true);

    if include_manifest {
        if let Some(manifest_repo) = get_manifest_repo_info(manifest, workspace_root) {
            match check_manifest_repo_branch(&manifest_repo, base_override) {
                Ok(Some((branch, repo_info))) => {
                    branch_groups.entry(branch).or_default().push(repo_info);
                }
                Ok(None) => {}
                Err(e) => {
                    Output::warning(&format!("Could not check manifest repo: {}", e));
                }
            }
        }
    }

    // One `-t` title describes ONE branch. Every repo sitting on some other
    // branch with commits ahead used to get a PR under that title too -- a
    // manifest parked on an old WIP branch opened a stray PR at every
    // `gr pr create`, titled after an unrelated feature. Scope to the branch
    // of the repo the command was run from; list the rest.
    let mut candidates: Vec<RepoInfo> = repos.clone();
    if let Some(m) = get_manifest_repo_info(manifest, workspace_root) {
        candidates.push(m);
    }
    let invoked_branch = invoked_from.and_then(|cwd| invoking_repo_branch(cwd, &candidates));
    let mut skipped_any = false;
    match scope_branch_groups(
        branch_groups,
        invoked_branch.as_deref(),
        repo_filter.is_some(),
    ) {
        PrScope::Refuse { groups } => {
            let listed: Vec<String> = groups
                .iter()
                .map(|(b, rs)| format!("{} ({})", b, rs.join(", ")))
                .collect();
            anyhow::bail!(
                "refusing: repos with commits ahead are on different branches -- {} -- and this was run from outside any repo, so there is no branch to scope to. One -t title cannot describe two branches. Run it from the repo whose branch you mean, or name the repos with --repo.",
                listed.join("; ")
            );
        }
        PrScope::Open { open, skipped } => {
            if !skipped.is_empty() {
                skipped_any = true;
                if !json {
                    for (repo, branch) in &skipped {
                        Output::info(&format!(
                            "not included: {} is on '{}', not this branch; pass --repo {} to include it",
                            repo, branch, repo
                        ));
                    }
                }
            }
            branch_groups = open;
        }
    }

    if branch_groups.is_empty() {
        if !json {
            if skipped_any {
                println!(
                    "Nothing to open on branch '{}'.",
                    invoked_branch.as_deref().unwrap_or("?")
                );
            } else {
                println!("No repositories have changes to create PRs for.");
            }
        } else {
            // Routed through the same helper as the terminal branch, because
            // production had TWO serialization sites and they disagreed about
            // the same inputs: this one hardcoded `success: true` for
            // zero-created/zero-failed while the terminal branch computed
            // `success: false` from that identical state. Both were already
            // shipping. A consumer could not rely on either answer, and which
            // one it got depended on how far the command happened to get.
            //
            // Found by Sentinel and Atlas independently at the v3 gate, after
            // two earlier rounds on this same PR fixed the same shape one
            // layer in each time -- a test name, then a construction site, now
            // an unrouted return. The recurring lesson is that "built in one
            // function" is a claim about EVERY production return, and the only
            // way to hold it is to enumerate them rather than to assert it.
            let result = pr_create_json_payload(&[], &[]);
            println!("{}", serde_json::to_string_pretty(&result)?);
        }
        return Ok(());
    }

    // Build ordered branch groups
    let groups: Vec<BranchGroup> = branch_groups
        .into_iter()
        .map(|(branch, repos)| BranchGroup { branch, repos })
        .collect();

    // Warn if PRs target main/master — may want a sprint branch (#418)
    // Skip warning when --base is explicit (the user knows what they're targeting)
    if !json && base_override.is_none() {
        let default_targets: Vec<&str> = groups
            .iter()
            .flat_map(|g| g.repos.iter())
            .filter(|r| matches!(r.target_branch(), "main" | "master"))
            .map(|r| r.target_branch())
            .collect();
        if !default_targets.is_empty() {
            Output::warning(
                "PR targets 'main'. If a sprint branch is active, use `gr target set <branch>` first.",
            );
        }
    }

    let multi_branch = groups.len() > 1;

    // All results across all branch groups
    let mut all_created_prs: Vec<(String, String, u64, String)> = Vec::new(); // (branch, repo, number, url)
    let mut all_failed_repos: Vec<(String, String)> = Vec::new(); // (repo, error)

    for group in &groups {
        let pr_title = title
            .map(|s| s.to_string())
            .unwrap_or_else(|| branch_to_title(&group.branch));

        if multi_branch && !json {
            Output::subheader(&format!("Branch: {}", group.branch));
        }

        // Push if requested (skip for preview)
        if push_first && !dry_run {
            if !multi_branch {
                Output::info("Pushing branches first...");
            }
            for repo in &group.repos {
                if let Ok(git_repo) = open_repo(&repo.absolute_path) {
                    let spinner = Output::spinner(&format!("Pushing {}...", repo.name));
                    match crate::git::remote::push_branch(
                        &git_repo,
                        &group.branch,
                        &repo.push_remote,
                        true,
                    ) {
                        Ok(()) => spinner.finish_with_message(format!("{}: pushed", repo.name)),
                        Err(e) => spinner
                            .finish_with_message(format!("{}: push failed - {}", repo.name, e)),
                    }
                }
            }
            println!();
        }

        // Preview mode
        if dry_run {
            Output::info(&format!("Branch: {}", group.branch));
            Output::info(&format!("Title: {}", pr_title));
            if let Some(pr_body) = body {
                Output::info(&format!("Body: {}", pr_body));
            }
            if draft {
                Output::info("Type: Draft PR");
            }
            println!();

            Output::subheader("Repositories that would create PRs:");
            for repo in &group.repos {
                let target = base_override.unwrap_or_else(|| repo.target_branch());
                println!(
                    "  - {} ({}/{}) → {}",
                    repo.name, repo.owner, repo.repo, target
                );
            }
            println!();
            continue;
        }

        // Create PRs for each repo in this branch group
        for repo in &group.repos {
            let target = base_override.unwrap_or_else(|| repo.target_branch());
            let platform =
                get_platform_adapter(repo.platform_type, repo.platform_base_url.as_deref());

            let spinner = Output::spinner(&format!("Creating PR for {}...", repo.name));

            // `target` above already resolved --base against the stored
            // target. Re-deriving it here from the manifest asked about a
            // branch the operator never named -- and the two only disagree
            // when --base was passed, which is exactly when the stored
            // target is stale.
            match platform
                .check_branch_exists(&repo.owner, &repo.repo, target)
                .await
            {
                Ok(false) => {
                    spinner.finish_with_message(format!(
                        "{}: skipped — base branch '{}' does not exist on remote",
                        repo.name, target
                    ));
                    all_failed_repos.push((
                        repo.name.clone(),
                        format!("base branch '{}' not found on remote", target),
                    ));
                    continue;
                }
                Err(e) => {
                    debug!(
                        repo = repo.name.as_str(),
                        base = target,
                        error = %e,
                        "Could not verify base branch; proceeding to API call"
                    );
                }
                Ok(true) => {}
            }

            match platform
                .create_pull_request(
                    &repo.owner,
                    &repo.repo,
                    &group.branch,
                    target,
                    &pr_title,
                    body,
                    draft,
                )
                .await
            {
                Ok(pr) => {
                    spinner.finish_with_message(format!(
                        "{}: created PR #{} - {}",
                        repo.name, pr.number, pr.url
                    ));
                    all_created_prs.push((
                        group.branch.clone(),
                        repo.name.clone(),
                        pr.number,
                        pr.url.clone(),
                    ));
                }
                Err(e) => {
                    if let Ok(Some((new_owner, new_repo))) =
                        platform.resolve_repo(&repo.owner, &repo.repo).await
                    {
                        spinner.finish_with_message(format!(
                            "{}: repo renamed {}/{} → {}/{}, retrying...",
                            repo.name, repo.owner, repo.repo, new_owner, new_repo
                        ));
                        if let Ok(git_repo) = open_repo(&repo.absolute_path) {
                            let new_url = rewrite_remote_url(
                                &git_repo,
                                &repo.push_remote,
                                &repo.owner,
                                &repo.repo,
                                &new_owner,
                                &new_repo,
                            );
                            if let Some(url) = &new_url {
                                if set_remote_url(&git_repo, &repo.push_remote, url).is_ok() {
                                    Output::success(&format!(
                                        "{}: updated remote '{}' → {}",
                                        repo.name, repo.push_remote, url
                                    ));
                                }
                            }
                        }
                        match platform
                            .create_pull_request(
                                &new_owner,
                                &new_repo,
                                &group.branch,
                                target,
                                &pr_title,
                                body,
                                draft,
                            )
                            .await
                        {
                            Ok(pr) => {
                                Output::success(&format!(
                                    "{}: created PR #{} - {}",
                                    repo.name, pr.number, pr.url
                                ));
                                all_created_prs.push((
                                    group.branch.clone(),
                                    repo.name.clone(),
                                    pr.number,
                                    pr.url.clone(),
                                ));
                                Output::warning(&format!(
                                    "Update gripspace.yml: change {} URL to {}/{}.git",
                                    repo.name, new_owner, new_repo
                                ));
                            }
                            Err(e2) => {
                                spinner.finish_with_message(format!(
                                    "{}: retry failed - {}",
                                    repo.name, e2
                                ));
                                all_failed_repos.push((repo.name.clone(), e2.to_string()));
                            }
                        }
                    } else {
                        spinner.finish_with_message(format!("{}: failed - {}", repo.name, e));
                        all_failed_repos.push((repo.name.clone(), e.to_string()));
                    }
                }
            }
        }

        // Save state per branch
        if all_created_prs
            .iter()
            .any(|(b, _, _, _)| b == &group.branch)
        {
            let state_path = workspace_root.join(".gitgrip").join("state.json");
            let mut state = if state_path.exists() {
                let content = std::fs::read_to_string(&state_path)?;
                StateFile::parse(&content).unwrap_or_default()
            } else {
                StateFile::default()
            };

            if let Some((_, _, first_pr_number, _)) = all_created_prs
                .iter()
                .find(|(b, _, _, _)| b == &group.branch)
            {
                state.set_pr_for_branch(&group.branch, *first_pr_number);
            }

            let state_json = serde_json::to_string_pretty(&state)?;
            std::fs::write(&state_path, state_json)?;
        }
    }

    // Summary
    if dry_run {
        if !json {
            Output::warning("Run without --dry-run to actually create the PRs.");
        }
        return Ok(());
    }

    if json {
        let result = pr_create_json_payload(&all_created_prs, &all_failed_repos);
        println!("{}", serde_json::to_string_pretty(&result)?);
        // JSON mode keeps exit 0 and carries pass/fail in the body. That is
        // this repo's shipped convention, not a guess: `gr verify --json`
        // returns Ok before its own `exit(1)` (verify.rs:101), and
        // docs/PLAN-verify.md states the reason -- a caller who asked for JSON
        // is parsing the body by construction, and a non-zero exit makes a
        // `set -e` script die before it can read the answer it asked for.
        // `success` above already carries the truth.
        return Ok(());
    } else {
        println!();
        if all_created_prs.is_empty() && all_failed_repos.is_empty() {
            Output::warning("No PRs were created.");
        } else {
            if !all_created_prs.is_empty() {
                Output::success(&format!("Created {} PR(s):", all_created_prs.len()));
                for (branch, repo_name, pr_number, url) in &all_created_prs {
                    if multi_branch {
                        println!("  {} ({}): #{} - {}", repo_name, branch, pr_number, url);
                    } else {
                        println!("  {}: #{} - {}", repo_name, pr_number, url);
                    }
                }
            }
            if !all_failed_repos.is_empty() {
                if !all_created_prs.is_empty() {
                    println!();
                }
                Output::error(&format!(
                    "Failed to create {} PR(s):",
                    all_failed_repos.len()
                ));
                for (repo_name, error) in &all_failed_repos {
                    println!("  {}: {}", repo_name, error);
                }
            }
        }
    }

    // The exit status has to agree with what we just printed. Until now this
    // returned Ok(()) unconditionally, so a run that reported "Failed to
    // create N PR(s)" on stdout simultaneously told every caller gating on the
    // exit status that it had succeeded -- a script, CI, or an agent deciding
    // whether to continue reads the number, not the prose.
    //
    // The predicate was never missing: the --json branch a few lines above
    // already computes `success: !created.is_empty() && failed.is_empty()`.
    // The truth was computed and then discarded. This binds the return to the
    // failure half of that same expression, so the two reports cannot disagree.
    //
    // Deliberately NOT changed here: a run that creates nothing and fails
    // nothing still exits 0. That is the zero-match no-op question tracked in
    // #804/#836/#839, and folding it in would change the status of runs no
    // witness in this file covers. `--json` also keeps exit 0 -- see the
    // return above -- so this guards the human path, where the exit status is
    // the ONLY machine-readable signal the command emits.
    if !all_failed_repos.is_empty() {
        let names: Vec<&str> = all_failed_repos
            .iter()
            .map(|(repo, _)| repo.as_str())
            .collect();
        anyhow::bail!(
            "failed to create {} of {} pull request(s): {}",
            all_failed_repos.len(),
            all_failed_repos.len() + all_created_prs.len(),
            names.join(", ")
        );
    }

    Ok(())
}

/// Check if a branch has commits ahead of another branch
pub(crate) fn has_commits_ahead(
    repo: &Repository,
    branch: &str,
    base: &str,
) -> anyhow::Result<bool> {
    let local_ref = format!("refs/heads/{}", branch);
    let base_ref = format!("refs/remotes/origin/{}", base);

    let local = match repo.find_reference(&local_ref) {
        Ok(r) => r,
        Err(_) => return Ok(false),
    };

    let base_branch = match repo.find_reference(&base_ref) {
        Ok(r) => r,
        Err(_) => {
            // Try local base branch
            match repo.find_reference(&format!("refs/heads/{}", base)) {
                Ok(r) => r,
                // Neither remote nor local base ref exists — assume the branch
                // has changes worth including (e.g. repo hasn't fetched yet).
                Err(_) => return Ok(true),
            }
        }
    };

    let local_oid = local.target().ok_or_else(|| {
        anyhow::anyhow!(
            "Could not resolve branch '{}'. Ensure it exists and has at least one commit.",
            branch
        )
    })?;
    let base_oid = base_branch.target().ok_or_else(|| {
        anyhow::anyhow!(
            "Could not resolve base branch '{}'. Ensure it exists and has at least one commit.",
            base
        )
    })?;

    let (ahead, _behind) = repo.graph_ahead_behind(local_oid, base_oid)?;
    Ok(ahead > 0)
}

/// Check if the manifest repo has changes and return its branch name
fn check_manifest_repo_branch(
    repo: &RepoInfo,
    base_override: Option<&str>,
) -> anyhow::Result<Option<(String, RepoInfo)>> {
    let git_repo = open_repo(&repo.absolute_path)
        .map_err(|e| anyhow::anyhow!("Failed to open repo: {}", e))?;

    let current = get_current_branch(&git_repo)
        .map_err(|e| anyhow::anyhow!("Failed to get current branch: {}", e))?;

    let target = base_override.unwrap_or_else(|| repo.target_branch());

    // Skip if on target branch
    if current == target {
        return Ok(None);
    }

    let has_commits = has_commits_ahead(&git_repo, &current, target)
        .map_err(|e| anyhow::anyhow!("Failed to check commits: {}", e))?;

    // Commits ahead only, like every other repo. Uncommitted changes are not
    // PR content: a dirty manifest with nothing committed on the branch used
    // to qualify here.
    if has_commits {
        Ok(Some((current, repo.clone())))
    } else {
        Ok(None)
    }
}

/// Get authentication token for platform
#[allow(dead_code)]
pub fn get_token_for_platform(platform: &PlatformType) -> Option<String> {
    match platform {
        PlatformType::GitHub => std::env::var("GITHUB_TOKEN")
            .ok()
            .or_else(|| std::env::var("GH_TOKEN").ok()),
        PlatformType::GitLab => std::env::var("GITLAB_TOKEN").ok(),
        PlatformType::AzureDevOps => std::env::var("AZURE_DEVOPS_TOKEN").ok(),
        PlatformType::Bitbucket => std::env::var("BITBUCKET_TOKEN").ok(),
    }
}

/// Rewrite a remote URL to reflect a renamed repository.
///
/// Preserves the original scheme (SSH/HTTPS) and host by replacing
/// `old_owner/old_repo` with `new_owner/new_repo` in the existing URL.
fn rewrite_remote_url(
    repo: &Repository,
    remote_name: &str,
    old_owner: &str,
    old_repo: &str,
    new_owner: &str,
    new_repo: &str,
) -> Option<String> {
    let current = get_remote_url(repo, remote_name).ok()??;

    // SSH URLs use ":" as separator (git@host:owner/repo.git)
    // HTTPS URLs use "/" as separator (https://host/owner/repo.git)
    let old_ssh = format!(":{}/{}", old_owner, old_repo);
    let new_ssh = format!(":{}/{}", new_owner, new_repo);
    let old_https = format!("/{}/{}", old_owner, old_repo);
    let new_https = format!("/{}/{}", new_owner, new_repo);

    if current.contains(&old_ssh) {
        Some(current.replace(&old_ssh, &new_ssh))
    } else if current.contains(&old_https) {
        Some(current.replace(&old_https, &new_https))
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    fn setup_test_repo() -> (TempDir, Repository) {
        let temp = TempDir::new().unwrap();

        Command::new("git")
            .args(["init"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["config", "user.name", "Test User"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Create initial commit on main
        fs::write(temp.path().join("README.md"), "# Test").unwrap();
        Command::new("git")
            .args(["add", "README.md"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Initial commit"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let repo = crate::git::open_repo(temp.path()).unwrap();
        (temp, repo)
    }

    #[test]
    fn test_has_commits_ahead_returns_true_when_no_base_refs_exist() {
        let (temp, repo) = setup_test_repo();

        // Create a feature branch with a commit
        Command::new("git")
            .args(["checkout", "-b", "feat/test"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        fs::write(temp.path().join("feature.txt"), "new feature").unwrap();
        Command::new("git")
            .args(["add", "feature.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Add feature"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Check against a base branch that doesn't exist locally or remotely.
        // This simulates the scenario where origin/main hasn't been fetched.
        let result = has_commits_ahead(&repo, "feat/test", "nonexistent-base");
        assert!(result.is_ok());
        assert!(
            result.unwrap(),
            "Should return true when base refs are missing"
        );
    }

    #[test]
    fn test_has_commits_ahead_with_local_base() {
        let (temp, repo) = setup_test_repo();

        // Get the default branch name
        let output = Command::new("git")
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        let default_branch = String::from_utf8_lossy(&output.stdout).trim().to_string();

        // Create feature branch with a commit
        Command::new("git")
            .args(["checkout", "-b", "feat/test"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        fs::write(temp.path().join("feature.txt"), "new feature").unwrap();
        Command::new("git")
            .args(["add", "feature.txt"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "Add feature"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        // Local base ref exists — should detect the commit ahead
        let result = has_commits_ahead(&repo, "feat/test", &default_branch);
        assert!(result.is_ok());
        assert!(result.unwrap(), "Should detect commits ahead of local base");
    }

    #[test]
    fn test_has_commits_ahead_same_commit() {
        let (temp, repo) = setup_test_repo();

        let output = Command::new("git")
            .args(["rev-parse", "--abbrev-ref", "HEAD"])
            .current_dir(temp.path())
            .output()
            .unwrap();
        let default_branch = String::from_utf8_lossy(&output.stdout).trim().to_string();

        // Create feature branch but don't add any commits
        Command::new("git")
            .args(["checkout", "-b", "feat/no-changes"])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = has_commits_ahead(&repo, "feat/no-changes", &default_branch);
        assert!(result.is_ok());
        assert!(
            !result.unwrap(),
            "Should return false when no commits ahead"
        );
    }

    #[test]
    fn test_branch_to_title() {
        assert_eq!(branch_to_title("feat/my-feature"), "My feature");
        assert_eq!(branch_to_title("fix/bug-fix"), "Bug fix");
        assert_eq!(branch_to_title("chore/cleanup_task"), "Cleanup task");
        assert_eq!(branch_to_title("custom-branch"), "Custom branch");
    }

    #[test]
    fn test_rewrite_remote_url_https() {
        let (temp, repo) = setup_test_repo();
        Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/oldorg/oldrepo.git",
            ])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = rewrite_remote_url(&repo, "origin", "oldorg", "oldrepo", "neworg", "newrepo");
        assert_eq!(
            result,
            Some("https://github.com/neworg/newrepo.git".to_string())
        );
    }

    #[test]
    fn test_rewrite_remote_url_ssh() {
        let (temp, repo) = setup_test_repo();
        Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "git@github.com:oldorg/oldrepo.git",
            ])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = rewrite_remote_url(&repo, "origin", "oldorg", "oldrepo", "neworg", "newrepo");
        assert_eq!(
            result,
            Some("git@github.com:neworg/newrepo.git".to_string())
        );
    }

    #[test]
    fn test_rewrite_remote_url_ghe() {
        let (temp, repo) = setup_test_repo();
        Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.example.com/oldorg/oldrepo.git",
            ])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = rewrite_remote_url(&repo, "origin", "oldorg", "oldrepo", "neworg", "newrepo");
        assert_eq!(
            result,
            Some("https://github.example.com/neworg/newrepo.git".to_string())
        );
    }

    #[test]
    fn test_rewrite_remote_url_ghe_ssh() {
        let (temp, repo) = setup_test_repo();
        Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "git@ghe.corp.net:oldorg/oldrepo.git",
            ])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = rewrite_remote_url(&repo, "origin", "oldorg", "oldrepo", "neworg", "newrepo");
        assert_eq!(
            result,
            Some("git@ghe.corp.net:neworg/newrepo.git".to_string())
        );
    }

    #[test]
    fn test_rewrite_remote_url_no_match() {
        let (temp, repo) = setup_test_repo();
        Command::new("git")
            .args([
                "remote",
                "add",
                "origin",
                "https://github.com/different/repo.git",
            ])
            .current_dir(temp.path())
            .output()
            .unwrap();

        let result = rewrite_remote_url(&repo, "origin", "oldorg", "oldrepo", "neworg", "newrepo");
        assert_eq!(result, None);
    }
}

#[cfg(test)]
mod json_payload_tests {
    use super::pr_create_json_payload;

    /// THE WITNESS BOTH GATE REVIEWERS REQUIRED.
    ///
    /// `--json` keeps exit 0 on failure, and that is only defensible because
    /// the payload tells the truth. Before this, nothing pinned the payload:
    /// r1 and r2 independently mutated `success` to unconditional `true` and
    /// all three integration tests still passed. A scripted caller would have
    /// received process success AND payload success after the platform
    /// rejected the creation.
    ///
    /// Asserted on the SERIALIZED text, through the same
    /// `serde_json::to_string_pretty` call the command makes, rather than on
    /// the struct — the bytes on stdout are what a caller parses, and a field
    /// renamed or skipped in serialization would pass a struct-level check
    /// while breaking every consumer.
    #[test]
    fn a_failed_run_serializes_success_false_and_names_the_repo() {
        let created: Vec<(String, String, u64, String)> = vec![];
        let failed = vec![(
            "frontend".to_string(),
            "GitHub API 422: Validation Failed".to_string(),
        )];

        let out = serde_json::to_string_pretty(&pr_create_json_payload(&created, &failed)).unwrap();

        assert!(
            out.contains("\"success\": false"),
            "the payload must say the run failed; exit 0 is only safe while it does: {out}"
        );
        assert!(
            out.contains("\"repo\": \"frontend\""),
            "the failing repo must be named in the payload: {out}"
        );
        assert!(
            out.contains("422"),
            "the reason must survive into the payload: {out}"
        );
    }

    /// THE DISCRIMINATING CONTROL. Without it, `success: false` hardcoded
    /// would satisfy the witness above, and a caller could never tell a
    /// successful run from a failed one — the same defect sign-flipped.
    #[test]
    fn a_clean_run_serializes_success_true() {
        let created = vec![(
            "feat/thing".to_string(),
            "frontend".to_string(),
            11u64,
            "https://example.invalid/pull/11".to_string(),
        )];
        let failed: Vec<(String, String)> = vec![];

        let out = serde_json::to_string_pretty(&pr_create_json_payload(&created, &failed)).unwrap();

        assert!(
            out.contains("\"success\": true"),
            "a run with no failures must report success: {out}"
        );
    }

    /// The third state, and the one the scope note is about: nothing created
    /// and nothing failed is NOT a success. Pinned so that the deliberate
    /// choice to keep its EXIT status at 0 cannot be quietly read as a claim
    /// that the run succeeded.
    #[test]
    fn a_no_op_run_is_not_reported_as_success() {
        let created: Vec<(String, String, u64, String)> = vec![];
        let failed: Vec<(String, String)> = vec![];

        let out = serde_json::to_string_pretty(&pr_create_json_payload(&created, &failed)).unwrap();

        assert!(
            out.contains("\"success\": false"),
            "a run that created nothing has not succeeded, whatever its exit status: {out}"
        );
    }
}

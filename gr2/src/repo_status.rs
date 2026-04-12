use anyhow::{Context, Result};
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::spec::{read_workspace_spec, RepoSpec, UnitSpec};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RepoAction {
    CloneMissing,
    BlockPathConflict,
    BlockDirty,
    FastForward,
    ManualSync,
    NoChange,
}

impl RepoAction {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::CloneMissing => "clone_missing",
            Self::BlockPathConflict => "block_path_conflict",
            Self::BlockDirty => "block_dirty",
            Self::FastForward => "fast_forward",
            Self::ManualSync => "manual_sync",
            Self::NoChange => "no_change",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RepoScope {
    Shared,
    Unit,
}

impl RepoScope {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Shared => "shared",
            Self::Unit => "unit",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RepoStatusRow {
    pub scope: RepoScope,
    pub target: String,
    pub repo: String,
    pub path: PathBuf,
    pub action: RepoAction,
    pub branch: Option<String>,
    pub upstream: Option<String>,
    pub dirty: bool,
    pub ahead: u32,
    pub behind: u32,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RepoStatusReport {
    pub rows: Vec<RepoStatusRow>,
}

#[derive(Debug, Clone, Default)]
pub struct RepoStatusFilter {
    pub unit: Option<String>,
    pub repo: Option<String>,
}

impl RepoStatusReport {
    pub fn load(workspace_root: &Path, filter: &RepoStatusFilter) -> Result<Self> {
        let spec = read_workspace_spec(workspace_root)?;
        let mut rows = Vec::new();

        for repo in &spec.repos {
            if filter.repo.as_deref().is_some_and(|name| name != repo.name) {
                continue;
            }
            rows.push(classify_shared_repo(workspace_root, repo)?);
        }

        for unit in &spec.units {
            if filter.unit.as_deref().is_some_and(|name| name != unit.name) {
                continue;
            }
            for repo_name in &unit.repos {
                if filter.repo.as_deref().is_some_and(|name| name != repo_name) {
                    continue;
                }
                let repo = spec
                    .repos
                    .iter()
                    .find(|repo| repo.name == *repo_name)
                    .with_context(|| {
                        format!(
                            "unit '{}' references repo '{}' which is missing from workspace spec",
                            unit.name, repo_name
                        )
                    })?;
                rows.push(classify_unit_repo(workspace_root, unit, repo)?);
            }
        }

        rows.sort_by(|a, b| {
            a.scope
                .as_str()
                .cmp(b.scope.as_str())
                .then_with(|| a.target.cmp(&b.target))
                .then_with(|| a.repo.cmp(&b.repo))
        });

        Ok(Self { rows })
    }

    pub fn render_table(&self) -> String {
        if self.rows.is_empty() {
            return "RepoStatus\n- no repo targets matched\n".to_string();
        }

        let mut lines = vec![
            "RepoStatus".to_string(),
            "SCOPE\tTARGET\tREPO\tACTION\tBRANCH\tUPSTREAM\tSTATE\tREASON".to_string(),
        ];

        for row in &self.rows {
            let mut state = Vec::new();
            if row.dirty {
                state.push("dirty".to_string());
            }
            if row.ahead > 0 {
                state.push(format!("ahead={}", row.ahead));
            }
            if row.behind > 0 {
                state.push(format!("behind={}", row.behind));
            }
            if state.is_empty() {
                state.push("clean".to_string());
            }

            lines.push(format!(
                "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
                row.scope.as_str(),
                row.target,
                row.repo,
                row.action.as_str(),
                row.branch.as_deref().unwrap_or("-"),
                row.upstream.as_deref().unwrap_or("-"),
                state.join(","),
                row.reason
            ));
        }

        lines.join("\n")
    }
}

fn classify_shared_repo(workspace_root: &Path, repo: &RepoSpec) -> Result<RepoStatusRow> {
    let path = workspace_root.join(&repo.path);
    let mut row = base_row(
        RepoScope::Shared,
        repo.name.clone(),
        repo.name.clone(),
        path.clone(),
    );

    if !path.exists() {
        row.action = RepoAction::CloneMissing;
        row.reason = "shared repo path is absent".to_string();
        return Ok(row);
    }

    if !is_git_repo(&path)? {
        row.action = RepoAction::BlockPathConflict;
        row.reason = "shared repo path exists but is not a git repo".to_string();
        return Ok(row);
    }

    fill_git_status(&path, &mut row)?;
    classify_repo_state(&mut row, true);
    Ok(row)
}

fn classify_unit_repo(
    workspace_root: &Path,
    unit: &UnitSpec,
    repo: &RepoSpec,
) -> Result<RepoStatusRow> {
    let path = workspace_root.join(&unit.path).join(&repo.name);
    let mut row = base_row(
        RepoScope::Unit,
        unit.name.clone(),
        repo.name.clone(),
        path.clone(),
    );

    if !path.exists() {
        row.action = RepoAction::CloneMissing;
        row.reason = "unit repo checkout is absent".to_string();
        return Ok(row);
    }

    if !is_git_repo(&path)? {
        row.action = RepoAction::BlockPathConflict;
        row.reason = "unit repo path exists but is not a git repo".to_string();
        return Ok(row);
    }

    fill_git_status(&path, &mut row)?;
    classify_repo_state(&mut row, false);
    Ok(row)
}

fn base_row(scope: RepoScope, target: String, repo: String, path: PathBuf) -> RepoStatusRow {
    RepoStatusRow {
        scope,
        target,
        repo,
        path,
        action: RepoAction::NoChange,
        branch: None,
        upstream: None,
        dirty: false,
        ahead: 0,
        behind: 0,
        reason: String::new(),
    }
}

fn classify_repo_state(row: &mut RepoStatusRow, allow_ff: bool) {
    if row.dirty {
        row.action = RepoAction::BlockDirty;
        row.reason = "working tree is dirty; stop by default".to_string();
        return;
    }

    match (row.ahead, row.behind, row.upstream.is_some()) {
        (_, _, false) => {
            row.action = RepoAction::ManualSync;
            row.reason = "repo has no upstream tracking branch".to_string();
        }
        (0, 0, true) => {
            row.action = RepoAction::NoChange;
            row.reason = "repo is already aligned with upstream".to_string();
        }
        (0, behind, true) if behind > 0 && allow_ff => {
            row.action = RepoAction::FastForward;
            row.reason = format!("repo is behind upstream by {} commit(s)", behind);
        }
        (0, behind, true) if behind > 0 => {
            row.action = RepoAction::ManualSync;
            row.reason = format!(
                "repo is behind upstream by {} commit(s), but unit repos require explicit sync",
                behind
            );
        }
        (ahead, behind, true) if ahead > 0 && behind > 0 => {
            row.action = RepoAction::ManualSync;
            row.reason = format!(
                "repo diverged from upstream (ahead {}, behind {})",
                ahead, behind
            );
        }
        (ahead, 0, true) if ahead > 0 => {
            row.action = RepoAction::ManualSync;
            row.reason = format!("repo has {} local commit(s) ahead of upstream", ahead);
        }
        _ => {
            row.action = RepoAction::ManualSync;
            row.reason = "repo requires explicit inspection".to_string();
        }
    }
}

fn fill_git_status(repo_path: &Path, row: &mut RepoStatusRow) -> Result<()> {
    row.branch = git_stdout(repo_path, &["symbolic-ref", "--quiet", "--short", "HEAD"]).ok();
    row.upstream = git_stdout(
        repo_path,
        &[
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ],
    )
    .ok();
    row.dirty = !git_stdout(repo_path, &["status", "--porcelain"])?.is_empty();

    if row.upstream.is_some() {
        let counts = git_stdout(
            repo_path,
            &["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
        )?;
        let mut parts = counts.split_whitespace();
        row.ahead = parts.next().unwrap_or("0").parse().unwrap_or(0);
        row.behind = parts.next().unwrap_or("0").parse().unwrap_or(0);
    }

    Ok(())
}

fn is_git_repo(path: &Path) -> Result<bool> {
    let output = Command::new("git")
        .args(["rev-parse", "--is-inside-work-tree"])
        .current_dir(path)
        .output()
        .with_context(|| format!("run git rev-parse in {}", path.display()))?;

    Ok(output.status.success() && String::from_utf8_lossy(&output.stdout).trim() == "true")
}

fn git_stdout(repo_path: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(repo_path)
        .output()
        .with_context(|| format!("run git {:?} in {}", args, repo_path.display()))?;

    if !output.status.success() {
        anyhow::bail!(
            "git {:?} failed in {}: {}",
            args,
            repo_path.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

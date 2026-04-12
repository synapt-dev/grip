use anyhow::{Context, Result};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::lane::{lane_root_path, load_lane, LaneRecord};
use crate::spec::{read_workspace_spec, workspace_spec_path, WorkspaceSpec};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecStatusFilter {
    pub owner_unit: String,
    pub lane_name: String,
    pub repos: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecStatusEntry {
    pub repo: String,
    pub exec_path: PathBuf,
    pub path_kind: &'static str,
    pub branch: Option<String>,
    pub pr: Option<u64>,
    pub command_count: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecStatusReport {
    pub lane: LaneRecord,
    pub entries: Vec<ExecStatusEntry>,
}

impl ExecStatusReport {
    pub fn load(workspace_root: &Path, filter: &ExecStatusFilter) -> Result<Self> {
        let spec = load_workspace_spec_for_exec(workspace_root)?;
        let lane = load_lane(workspace_root, &filter.owner_unit, &filter.lane_name)?;

        let mut requested = filter.repos.clone();
        requested.sort();
        requested.dedup();

        for repo in &requested {
            if !lane.repos.iter().any(|member| member == repo) {
                anyhow::bail!(
                    "lane '{}' for unit '{}' does not include repo '{}'",
                    lane.lane_name,
                    lane.owner_unit,
                    repo
                );
            }
        }

        let repo_names = if requested.is_empty() {
            lane.repos.clone()
        } else {
            requested
        };

        let repo_specs = spec
            .repos
            .into_iter()
            .map(|repo| (repo.name.clone(), repo))
            .collect::<BTreeMap<_, _>>();

        let lane_root = lane_root_path(workspace_root, &lane.owner_unit, &lane.lane_name);
        let command_count = lane.exec_defaults.commands.len();

        let mut entries = Vec::new();
        for repo_name in repo_names {
            repo_specs
                .get(&repo_name)
                .with_context(|| format!("lane references unknown repo '{}'", repo_name))?;

            let lane_repo_path = lane_root.join("repos").join(&repo_name);
            let (exec_path, path_kind) = if lane_repo_path.exists() {
                (lane_repo_path, "lane")
            } else {
                (lane_repo_path, "missing")
            };

            let pr = lane
                .pr_associations
                .iter()
                .find(|pr| pr.repo == repo_name)
                .map(|pr| pr.number);

            entries.push(ExecStatusEntry {
                repo: repo_name.clone(),
                exec_path,
                path_kind,
                branch: lane.branch_map.get(&repo_name).cloned(),
                pr,
                command_count,
            });
        }

        Ok(Self { lane, entries })
    }

    pub fn render_table(&self) -> String {
        let mut out = String::new();
        out.push_str("gr2 exec status\n");
        out.push_str(&format!(
            "lane: {}/{}\n",
            self.lane.owner_unit, self.lane.lane_name
        ));
        out.push_str(&format!("type: {}\n", self.lane.lane_type.as_str()));
        out.push_str(&format!(
            "parallel: {}\n",
            if self.lane.exec_defaults.parallel {
                "true"
            } else {
                "false"
            }
        ));
        out.push_str(&format!(
            "fail_fast: {}\n",
            if self.lane.exec_defaults.fail_fast {
                "true"
            } else {
                "false"
            }
        ));

        if self.lane.exec_defaults.commands.is_empty() {
            out.push_str("commands: none\n");
        } else {
            out.push_str("commands:\n");
            for command in &self.lane.exec_defaults.commands {
                out.push_str(&format!("- {}\n", command));
            }
        }

        if self.entries.is_empty() {
            out.push_str("repos: none");
            return out;
        }

        out.push_str("repos:\n");
        out.push_str("REPO PATH_KIND EXEC_PATH BRANCH PR COMMANDS\n");
        for entry in &self.entries {
            out.push_str(&format!(
                "{} {} {} {} {} {}\n",
                entry.repo,
                entry.path_kind,
                entry.exec_path.display(),
                entry.branch.as_deref().unwrap_or("-"),
                entry
                    .pr
                    .map(|pr| pr.to_string())
                    .unwrap_or_else(|| "-".to_string()),
                entry.command_count
            ));
        }
        out.trim_end().to_string()
    }
}

fn load_workspace_spec_for_exec(workspace_root: &Path) -> Result<WorkspaceSpec> {
    let spec_path = workspace_spec_path(workspace_root);
    if !spec_path.exists() {
        anyhow::bail!(
            "workspace spec missing at {}; run 'gr2 spec show' or write .grip/workspace_spec.toml first",
            spec_path.display()
        );
    }
    read_workspace_spec(workspace_root)
}

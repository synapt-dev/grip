use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use crate::spec::{read_workspace_spec, workspace_spec_path, WorkspaceSpec};

pub const LANE_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LaneRecord {
    pub schema_version: u32,
    pub lane_id: String,
    pub lane_name: String,
    pub owner_unit: String,
    pub lane_type: LaneType,
    pub repos: Vec<String>,
    #[serde(default)]
    pub branch_map: BTreeMap<String, String>,
    pub creation_source: String,
    pub context: LaneContextRoots,
    pub exec_defaults: LaneExecDefaults,
    #[serde(default)]
    pub pr_associations: Vec<LanePrAssociation>,
    pub recovery: LaneRecoveryState,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LaneType {
    Home,
    Feature,
    Review,
    Scratch,
}

impl LaneType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Home => "home",
            Self::Feature => "feature",
            Self::Review => "review",
            Self::Scratch => "scratch",
        }
    }
}

impl std::str::FromStr for LaneType {
    type Err = anyhow::Error;

    fn from_str(value: &str) -> Result<Self> {
        match value {
            "home" => Ok(Self::Home),
            "feature" => Ok(Self::Feature),
            "review" => Ok(Self::Review),
            "scratch" => Ok(Self::Scratch),
            other => anyhow::bail!(
                "unknown lane type '{}': expected home, feature, review, or scratch",
                other
            ),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LaneContextRoots {
    #[serde(default)]
    pub shared_roots: Vec<String>,
    #[serde(default)]
    pub private_roots: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LaneExecDefaults {
    #[serde(default)]
    pub commands: Vec<String>,
    pub fail_fast: bool,
    pub parallel: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LanePrAssociation {
    pub repo: String,
    pub number: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct LaneRecoveryState {
    #[serde(default)]
    pub autostash_refs: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaneCreateRequest {
    pub name: String,
    pub owner_unit: String,
    pub lane_type: LaneType,
    pub repos: Vec<String>,
    pub branch_map: BTreeMap<String, String>,
    pub shared_context: Vec<String>,
    pub private_context: Vec<String>,
    pub exec_commands: Vec<String>,
    pub creation_source: String,
    pub pr_associations: Vec<LanePrAssociation>,
    pub parallel: bool,
    pub fail_fast: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaneSummary {
    pub owner_unit: String,
    pub lane_name: String,
    pub lane_type: LaneType,
    pub repo_count: usize,
}

pub fn create_lane(workspace_root: &Path, request: LaneCreateRequest) -> Result<LaneRecord> {
    validate_lane_name(&request.name)?;
    validate_lane_name(&request.owner_unit)?;

    let spec = load_workspace_spec_for_lanes(workspace_root)?;
    let unit = spec
        .units
        .iter()
        .find(|unit| unit.name == request.owner_unit)
        .with_context(|| format!("unit '{}' not found in workspace spec", request.owner_unit))?;

    let known_repos = spec
        .repos
        .iter()
        .map(|repo| repo.name.clone())
        .collect::<BTreeSet<_>>();

    let mut repos = if request.repos.is_empty() {
        unit.repos.clone()
    } else {
        request.repos.clone()
    };
    repos.sort();
    repos.dedup();

    if repos.is_empty() {
        anyhow::bail!(
            "lane '{}' has no repo membership; pass --repo or add repos to unit '{}' in workspace spec",
            request.name,
            request.owner_unit
        );
    }

    for repo in &repos {
        if !known_repos.contains(repo) {
            anyhow::bail!("lane '{}' references unknown repo '{}'", request.name, repo);
        }
    }

    for repo in request.branch_map.keys() {
        if !repos.iter().any(|member| member == repo) {
            anyhow::bail!(
                "lane '{}' branch map references repo '{}' outside lane membership",
                request.name,
                repo
            );
        }
    }

    for pr in &request.pr_associations {
        if !repos.iter().any(|repo| repo == &pr.repo) {
            anyhow::bail!(
                "lane '{}' PR association references repo '{}' outside lane membership",
                request.name,
                pr.repo
            );
        }
    }

    let metadata_path = lane_metadata_path(workspace_root, &request.owner_unit, &request.name);
    if metadata_path.exists() {
        anyhow::bail!(
            "lane '{}' for unit '{}' already exists",
            request.name,
            request.owner_unit
        );
    }

    let lane_root = lane_root_path(workspace_root, &request.owner_unit, &request.name);
    fs::create_dir_all(lane_root.join("repos"))
        .with_context(|| format!("create lane repos directory {}", lane_root.display()))?;
    fs::create_dir_all(lane_root.join("context"))
        .with_context(|| format!("create lane context directory {}", lane_root.display()))?;
    fs::create_dir_all(
        metadata_path
            .parent()
            .context("lane metadata parent missing")?,
    )
    .with_context(|| format!("create lane state directory {}", metadata_path.display()))?;

    let shared_roots = merge_roots(
        vec!["config".to_string(), ".grip/context/shared".to_string()],
        request.shared_context,
    );
    let private_roots = merge_roots(
        vec![
            format!("agents/{}/home/context", request.owner_unit),
            format!(
                "agents/{}/lanes/{}/context",
                request.owner_unit, request.name
            ),
        ],
        request.private_context,
    );

    let record = LaneRecord {
        schema_version: LANE_SCHEMA_VERSION,
        lane_id: format!("{}:{}", request.owner_unit, request.name),
        lane_name: request.name,
        owner_unit: request.owner_unit,
        lane_type: request.lane_type,
        repos,
        branch_map: request.branch_map,
        creation_source: request.creation_source,
        context: LaneContextRoots {
            shared_roots,
            private_roots,
        },
        exec_defaults: LaneExecDefaults {
            commands: request.exec_commands,
            fail_fast: request.fail_fast,
            parallel: request.parallel,
        },
        pr_associations: request.pr_associations,
        recovery: LaneRecoveryState::default(),
    };

    let content = toml::to_string_pretty(&record).context("serialize lane record")?;
    fs::write(&metadata_path, content)
        .with_context(|| format!("write lane metadata to {}", metadata_path.display()))?;

    Ok(record)
}

pub fn list_lanes(workspace_root: &Path, owner_filter: Option<&str>) -> Result<Vec<LaneSummary>> {
    let state_root = lane_state_root(workspace_root);
    if !state_root.exists() {
        return Ok(Vec::new());
    }

    let mut lanes = Vec::new();
    for owner_entry in fs::read_dir(&state_root)? {
        let owner_entry = owner_entry?;
        if !owner_entry.file_type()?.is_dir() {
            continue;
        }

        let owner_unit = owner_entry.file_name().to_string_lossy().into_owned();
        if let Some(filter) = owner_filter {
            if filter != owner_unit {
                continue;
            }
        }

        for lane_entry in fs::read_dir(owner_entry.path())? {
            let lane_entry = lane_entry?;
            if !lane_entry.file_type()?.is_file() {
                continue;
            }

            if lane_entry.path().extension().and_then(|ext| ext.to_str()) != Some("toml") {
                continue;
            }

            let record = load_lane_record_path(&lane_entry.path())?;
            lanes.push(LaneSummary {
                owner_unit: record.owner_unit,
                lane_name: record.lane_name,
                lane_type: record.lane_type,
                repo_count: record.repos.len(),
            });
        }
    }

    lanes.sort_by(|left, right| {
        left.owner_unit
            .cmp(&right.owner_unit)
            .then_with(|| left.lane_name.cmp(&right.lane_name))
    });
    Ok(lanes)
}

pub fn show_lane(workspace_root: &Path, owner_unit: &str, lane_name: &str) -> Result<String> {
    let path = lane_metadata_path(workspace_root, owner_unit, lane_name);
    fs::read_to_string(&path).with_context(|| format!("read lane metadata from {}", path.display()))
}

pub fn remove_lane(workspace_root: &Path, owner_unit: &str, lane_name: &str) -> Result<()> {
    let metadata_path = lane_metadata_path(workspace_root, owner_unit, lane_name);
    if !metadata_path.exists() {
        anyhow::bail!("lane '{}' for unit '{}' not found", lane_name, owner_unit);
    }

    fs::remove_file(&metadata_path)
        .with_context(|| format!("remove lane metadata {}", metadata_path.display()))?;

    let owner_state_root = lane_state_root(workspace_root).join(owner_unit);
    if owner_state_root.exists() && owner_state_root.read_dir()?.next().is_none() {
        fs::remove_dir(&owner_state_root).with_context(|| {
            format!(
                "remove empty lane owner state {}",
                owner_state_root.display()
            )
        })?;
    }

    let lane_root = lane_root_path(workspace_root, owner_unit, lane_name);
    if lane_root.exists() {
        fs::remove_dir_all(&lane_root)
            .with_context(|| format!("remove lane root {}", lane_root.display()))?;
    }

    Ok(())
}

pub fn render_lane_table(lanes: &[LaneSummary]) -> String {
    if lanes.is_empty() {
        return "No gr2 lanes registered.".to_string();
    }

    let mut out = String::from("Lanes\nOWNER NAME TYPE REPOS\n");
    for lane in lanes {
        out.push_str(&format!(
            "{} {} {} {}\n",
            lane.owner_unit,
            lane.lane_name,
            lane.lane_type.as_str(),
            lane.repo_count
        ));
    }
    out.trim_end().to_string()
}

fn load_workspace_spec_for_lanes(workspace_root: &Path) -> Result<WorkspaceSpec> {
    let spec_path = workspace_spec_path(workspace_root);
    if spec_path.exists() {
        read_workspace_spec(workspace_root)
    } else {
        WorkspaceSpec::from_workspace(workspace_root)
    }
}

fn load_lane_record_path(path: &Path) -> Result<LaneRecord> {
    let content = fs::read_to_string(path)
        .with_context(|| format!("read lane metadata from {}", path.display()))?;
    toml::from_str(&content).context("parse lane metadata")
}

fn merge_roots(defaults: Vec<String>, extras: Vec<String>) -> Vec<String> {
    let mut merged = defaults;
    for extra in extras {
        if !merged.iter().any(|existing| existing == &extra) {
            merged.push(extra);
        }
    }
    merged
}

pub fn lane_state_root(workspace_root: &Path) -> PathBuf {
    workspace_root.join(".grip/state/lanes")
}

pub fn lane_metadata_path(workspace_root: &Path, owner_unit: &str, lane_name: &str) -> PathBuf {
    lane_state_root(workspace_root)
        .join(owner_unit)
        .join(format!("{}.toml", lane_name))
}

pub fn lane_root_path(workspace_root: &Path, owner_unit: &str, lane_name: &str) -> PathBuf {
    workspace_root
        .join("agents")
        .join(owner_unit)
        .join("lanes")
        .join(lane_name)
}

pub fn validate_lane_name(name: &str) -> Result<()> {
    if name.is_empty() {
        anyhow::bail!("lane name must not be empty");
    }

    if !name
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' || ch == '.')
    {
        anyhow::bail!(
            "invalid lane name '{}': use only ASCII letters, numbers, '.', '_' or '-'",
            name
        );
    }

    Ok(())
}

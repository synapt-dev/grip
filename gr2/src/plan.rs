use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use crate::spec::{
    read_workspace_spec, workspace_spec_path, write_workspace_spec, LinkKind, LinkSpec, UnitSpec,
    WorkspaceSpec,
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExecutionPlan {
    pub operations: Vec<PlanOperation>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanBuild {
    pub spec: WorkspaceSpec,
    pub plan: ExecutionPlan,
    pub generated_spec: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PlanOperation {
    pub unit_name: String,
    pub operation: OperationType,
    #[serde(default)]
    pub parameters: BTreeMap<String, String>,
    pub preview: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OperationType {
    Clone,
    Configure,
    Link,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanGuardReport {
    pub warnings: Vec<String>,
    pub requires_confirmation: bool,
}

impl ExecutionPlan {
    pub fn from_workspace_spec(workspace_root: &Path) -> Result<PlanBuild> {
        let spec_path = workspace_spec_path(workspace_root);
        let (spec, generated_spec) = if spec_path.exists() {
            (read_workspace_spec(workspace_root)?, false)
        } else {
            let generated = WorkspaceSpec::from_workspace(workspace_root)?;
            write_workspace_spec(workspace_root, &generated)?;
            (generated, true)
        };

        spec.validate_for_plan()?;

        let mut operations = Vec::new();

        for unit in &spec.units {
            let unit_root = workspace_root.join(&unit.path);
            let unit_toml = unit_root.join("unit.toml");

            if !unit_toml.exists() {
                let mut parameters = BTreeMap::new();
                parameters.insert("path".to_string(), unit.path.clone());
                parameters.insert("repos".to_string(), unit.repos.join(","));
                operations.push(PlanOperation {
                    unit_name: unit.name.clone(),
                    operation: OperationType::Clone,
                    parameters,
                    preview: format!("clone unit '{}' into {}", unit.name, unit.path),
                });
                // Fall through to check links (they'll all be missing since the unit
                // doesn't exist yet, but they should be planned alongside the Clone)
            } else {
                let expected_path = format!("agents/{}", unit.name);
                if unit.path != expected_path {
                    let mut parameters = BTreeMap::new();
                    parameters.insert("path".to_string(), unit.path.clone());
                    parameters.insert("repos".to_string(), unit.repos.join(","));
                    operations.push(PlanOperation {
                        unit_name: unit.name.clone(),
                        operation: OperationType::Configure,
                        parameters,
                        preview: format!("reconfigure unit '{}' to match {}", unit.name, unit.path),
                    });
                }
            }

            // Check declared links against filesystem
            for link in &unit.links {
                let dest_path = unit_root.join(&link.dest);
                if !dest_path.exists() {
                    let mut parameters = BTreeMap::new();
                    parameters.insert("src".to_string(), link.src.clone());
                    parameters.insert("dest".to_string(), link.dest.clone());
                    parameters.insert("kind".to_string(), link.kind.as_str().to_string());
                    operations.push(PlanOperation {
                        unit_name: unit.name.clone(),
                        operation: OperationType::Link,
                        parameters,
                        preview: format!(
                            "{} {} -> {}/{}",
                            link.kind.as_str(),
                            link.src,
                            unit.path,
                            link.dest
                        ),
                    });
                }
            }
        }

        Ok(PlanBuild {
            spec,
            plan: Self { operations },
            generated_spec,
        })
    }

    pub fn apply(&self, workspace_root: &Path, spec: &WorkspaceSpec) -> Result<Vec<String>> {
        let mut applied = Vec::new();

        for operation in &self.operations {
            let unit_spec = spec
                .units
                .iter()
                .find(|unit| unit.name == operation.unit_name)
                .with_context(|| {
                    format!(
                        "execution plan references unknown unit '{}'",
                        operation.unit_name
                    )
                })?;

            match operation.operation {
                OperationType::Clone => {
                    materialize_unit(workspace_root, unit_spec, spec)?;
                    applied.push(format!(
                        "cloned unit '{}' into {}",
                        unit_spec.name, unit_spec.path
                    ));
                }
                OperationType::Configure => {
                    materialize_unit(workspace_root, unit_spec, spec)?;
                    applied.push(format!(
                        "configured unit '{}' at {}",
                        unit_spec.name, unit_spec.path
                    ));
                }
                OperationType::Link => {
                    let src = operation
                        .parameters
                        .get("src")
                        .context("link operation missing 'src' parameter")?;
                    let dest = operation
                        .parameters
                        .get("dest")
                        .context("link operation missing 'dest' parameter")?;
                    let kind_str = operation
                        .parameters
                        .get("kind")
                        .map(|s| s.as_str())
                        .unwrap_or("symlink");
                    let kind = kind_str.parse::<LinkKind>()?;

                    let link_spec = LinkSpec {
                        src: src.clone(),
                        dest: dest.clone(),
                        kind,
                    };
                    apply_link(workspace_root, unit_spec, &link_spec)?;
                    applied.push(format!(
                        "{} {} -> {}/{}",
                        kind_str, src, unit_spec.path, dest
                    ));
                }
            }
        }

        if !applied.is_empty() {
            record_apply_state(workspace_root, &applied)?;
        }

        Ok(applied)
    }

    pub fn guard_for_apply(
        &self,
        workspace_root: &Path,
        spec: &WorkspaceSpec,
        assume_yes: bool,
    ) -> Result<PlanGuardReport> {
        let mut warnings = Vec::new();

        for operation in &self.operations {
            // Resolve the unit's actual path from the spec, not from parameters
            let unit_path = spec
                .units
                .iter()
                .find(|u| u.name == operation.unit_name)
                .map(|u| u.path.as_str())
                .unwrap_or_else(|| {
                    operation
                        .parameters
                        .get("path")
                        .map(|s| s.as_str())
                        .unwrap_or("")
                });

            match operation.operation {
                OperationType::Link => {
                    if let Some(dest) = operation.parameters.get("dest") {
                        let dest_path = workspace_root.join(unit_path).join(dest);
                        if dest_path.exists() {
                            anyhow::bail!(
                                "refusing to apply plan: link destination already exists for unit '{}': {}",
                                operation.unit_name,
                                dest_path.display()
                            );
                        }
                    }
                }
                _ => {
                    let path = workspace_root.join(unit_path);

                    if path.join(".git").exists() {
                        warnings.push(format!(
                            "unit '{}' has a git checkout at {} with possible uncommitted changes; inspect before apply",
                            operation.unit_name,
                            path.display()
                        ));
                    }
                }
            }
        }

        Ok(PlanGuardReport {
            warnings,
            requires_confirmation: self.operations.len() > 3 && !assume_yes,
        })
    }

    pub fn render_table(&self) -> String {
        if self.operations.is_empty() {
            return "ExecutionPlan\n- no changes required\n".to_string();
        }

        let mut lines = vec![
            "ExecutionPlan".to_string(),
            "UNIT\tOPERATION\tPREVIEW".to_string(),
        ];
        for operation in &self.operations {
            lines.push(format!(
                "{}\t{}\t{}",
                operation.unit_name,
                operation.operation.as_str(),
                operation.preview
            ));
        }
        lines.join("\n")
    }
}

impl OperationType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Clone => "clone",
            Self::Configure => "configure",
            Self::Link => "link",
        }
    }
}

fn apply_link(workspace_root: &Path, unit: &UnitSpec, link: &LinkSpec) -> Result<()> {
    let src_path = workspace_root.join(&link.src);
    let unit_root = workspace_root.join(&unit.path);
    let dest_path = unit_root.join(&link.dest);

    if !src_path.exists() {
        anyhow::bail!(
            "link source does not exist: {} (for unit '{}')",
            src_path.display(),
            unit.name
        );
    }

    if let Some(parent) = dest_path.parent() {
        fs::create_dir_all(parent).with_context(|| {
            format!(
                "create parent directory for link destination {}",
                dest_path.display()
            )
        })?;
    }

    match link.kind {
        LinkKind::Symlink => {
            let abs_src = fs::canonicalize(&src_path).with_context(|| {
                format!(
                    "resolve absolute path for link source {}",
                    src_path.display()
                )
            })?;
            #[cfg(unix)]
            std::os::unix::fs::symlink(&abs_src, &dest_path).with_context(|| {
                format!(
                    "create symlink {} -> {}",
                    dest_path.display(),
                    abs_src.display()
                )
            })?;
            #[cfg(windows)]
            {
                if abs_src.is_dir() {
                    std::os::windows::fs::symlink_dir(&abs_src, &dest_path)
                } else {
                    std::os::windows::fs::symlink_file(&abs_src, &dest_path)
                }
                .with_context(|| {
                    format!(
                        "create symlink {} -> {}",
                        dest_path.display(),
                        abs_src.display()
                    )
                })?;
            }
        }
        LinkKind::Copy => {
            if src_path.is_dir() {
                copy_dir_recursive(&src_path, &dest_path).with_context(|| {
                    format!(
                        "copy directory {} -> {}",
                        src_path.display(),
                        dest_path.display()
                    )
                })?;
            } else {
                fs::copy(&src_path, &dest_path).with_context(|| {
                    format!(
                        "copy file {} -> {}",
                        src_path.display(),
                        dest_path.display()
                    )
                })?;
            }
        }
    }

    Ok(())
}

fn copy_dir_recursive(src: &Path, dest: &Path) -> Result<()> {
    fs::create_dir_all(dest)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let entry_dest = dest.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir_recursive(&entry.path(), &entry_dest)?;
        } else {
            fs::copy(entry.path(), &entry_dest)?;
        }
    }
    Ok(())
}

fn materialize_unit(workspace_root: &Path, unit: &UnitSpec, spec: &WorkspaceSpec) -> Result<()> {
    let unit_root = workspace_root.join(&unit.path);
    fs::create_dir_all(&unit_root)
        .with_context(|| format!("create unit directory {}", unit_root.display()))?;
    fs::write(unit_root.join("unit.toml"), render_unit_toml(unit))
        .with_context(|| format!("write unit metadata for '{}'", unit.name))?;

    // Clone repos declared in the unit's repos list
    for repo_name in &unit.repos {
        let repo_spec = spec
            .repos
            .iter()
            .find(|r| r.name == *repo_name)
            .with_context(|| {
                format!(
                    "unit '{}' references repo '{}' which is not in the workspace spec",
                    unit.name, repo_name
                )
            })?;

        let clone_dest = unit_root.join(repo_name);
        if clone_dest.exists() {
            continue; // Already cloned, skip
        }

        let output = std::process::Command::new("git")
            .args(["clone", &repo_spec.url])
            .arg(&clone_dest)
            .output()
            .with_context(|| {
                format!(
                    "run git clone for repo '{}' into {}",
                    repo_name,
                    clone_dest.display()
                )
            })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::bail!(
                "git clone failed for repo '{}' into {}: {}",
                repo_name,
                clone_dest.display(),
                stderr.trim()
            );
        }
    }

    Ok(())
}

fn record_apply_state(workspace_root: &Path, actions: &[String]) -> Result<()> {
    let state_dir = workspace_root.join(".grip/state");
    fs::create_dir_all(&state_dir)
        .with_context(|| format!("create state directory {}", state_dir.display()))?;

    let timestamp = chrono::Utc::now().to_rfc3339();
    let mut content = format!("# Last apply: {}\n\n", timestamp);
    content.push_str("[[applied]]\n");
    content.push_str(&format!("timestamp = \"{}\"\n", timestamp));
    content.push_str(&format!(
        "actions = [{}]\n",
        actions
            .iter()
            .map(|a| format!("\"{}\"", a.replace('"', "\\\"")))
            .collect::<Vec<_>>()
            .join(", ")
    ));

    let state_path = state_dir.join("applied.toml");

    // Append to existing state file
    if state_path.exists() {
        let existing = fs::read_to_string(&state_path)?;
        content = format!("{}\n{}", existing.trim_end(), content);
    }

    fs::write(&state_path, content)
        .with_context(|| format!("write apply state to {}", state_path.display()))?;

    Ok(())
}

fn render_unit_toml(unit: &UnitSpec) -> String {
    let repos = if unit.repos.is_empty() {
        "[]".to_string()
    } else {
        format!(
            "[{}]",
            unit.repos
                .iter()
                .map(|repo| format!("\"{}\"", repo))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };

    format!(
        "name = \"{}\"\nkind = \"unit\"\nrepos = {}\n",
        unit.name, repos
    )
}

use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

use crate::spec::{read_workspace_spec, workspace_spec_path, write_workspace_spec, WorkspaceSpec};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExecutionPlan {
    pub operations: Vec<PlanOperation>,
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
    pub fn from_workspace_spec(workspace_root: &Path) -> Result<(WorkspaceSpec, Self)> {
        let spec_path = workspace_spec_path(workspace_root);
        let spec = if spec_path.exists() {
            read_workspace_spec(workspace_root)?
        } else {
            let generated = WorkspaceSpec::from_workspace(workspace_root)?;
            write_workspace_spec(workspace_root, &generated)?;
            generated
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
                continue;
            }

            let expected_path = format!("agents/{}", unit.name);
            if unit.path != expected_path || !unit.repos.is_empty() {
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

        Ok((spec, Self { operations }))
    }

    pub fn guard_for_apply(
        &self,
        workspace_root: &Path,
        assume_yes: bool,
    ) -> Result<PlanGuardReport> {
        let mut warnings = Vec::new();

        for operation in &self.operations {
            let path = operation
                .parameters
                .get("path")
                .map(|value| workspace_root.join(value))
                .unwrap_or_else(|| workspace_root.join(format!("agents/{}", operation.unit_name)));

            if matches!(operation.operation, OperationType::Link) && path.exists() {
                anyhow::bail!(
                    "refusing to apply plan: link operation for '{}' would overwrite existing directory {}",
                    operation.unit_name,
                    path.display()
                );
            }

            if path.join(".git").exists() {
                warnings.push(format!(
                    "unit '{}' has a git checkout at {} with possible uncommitted changes; inspect before apply",
                    operation.unit_name,
                    path.display()
                ));
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

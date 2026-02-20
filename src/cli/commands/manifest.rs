//! Manifest operations (import, sync)
//!
//! Handles conversion between git-repo XML manifests and gitgrip YAML manifests.

use crate::cli::output::Output;
use crate::core::repo_manifest::XmlManifest;
use std::path::Path;

/// Import a git-repo XML manifest and convert to gitgrip YAML
pub fn run_manifest_import(path: &str, output_path: Option<&str>) -> anyhow::Result<()> {
    let xml_path = Path::new(path);
    if !xml_path.exists() {
        anyhow::bail!("XML manifest not found: {}", path);
    }

    Output::header("Importing git-repo manifest...");
    println!();

    let xml_manifest = XmlManifest::parse_file(xml_path)?;
    let result = xml_manifest.to_manifest()?;

    // Print summary
    Output::info(&format!(
        "{} total projects, {} Gerrit (skipped), {} non-Gerrit (imported)",
        result.total_projects, result.gerrit_skipped, result.non_gerrit_imported
    ));

    for (platform, count) in &result.platform_counts {
        Output::info(&format!("  {}: {} repos", platform, count));
    }

    // Serialize to YAML
    let yaml = serde_yaml::to_string(&result.manifest)?;

    // Write output
    let dest = output_path.unwrap_or("gripspace.yml");
    std::fs::write(dest, &yaml)?;

    println!();
    Output::success(&format!("Written: {}", dest));

    Ok(())
}

/// Re-sync gitgrip YAML from .repo/ manifest XML
pub fn run_manifest_sync(workspace_root: &std::path::PathBuf) -> anyhow::Result<()> {
    // Find the XML manifest
    let repo_dir = workspace_root.join(".repo");
    let xml_path = repo_dir.join("manifest.xml");

    if !xml_path.exists() {
        anyhow::bail!("No .repo/manifest.xml found. Are you in a repo-managed workspace?");
    }

    Output::header("Syncing manifest from .repo/...");
    println!();

    let xml_manifest = XmlManifest::parse_file(&xml_path)?;
    let result = xml_manifest.to_manifest()?;

    Output::info(&format!(
        "{} total projects, {} Gerrit (skipped), {} non-Gerrit (imported)",
        result.total_projects, result.gerrit_skipped, result.non_gerrit_imported
    ));

    // Write to .repo/manifests/gripspace.yml (with legacy mirror)
    let yaml = serde_yaml::to_string(&result.manifest)?;
    let manifests_dir = repo_dir.join("manifests");
    let yaml_path = manifests_dir.join("gripspace.yml");
    std::fs::write(&yaml_path, &yaml)?;
    std::fs::write(manifests_dir.join("manifest.yaml"), &yaml)?;

    println!();
    Output::success(&format!("Updated: {}", yaml_path.display()));

    Ok(())
}

/// Show manifest schema specification
pub fn run_manifest_schema(format: &str) -> anyhow::Result<()> {
    let schema = include_str!("../../../docs/manifest-schema.yaml");

    match format {
        "yaml" => {
            println!("{}", schema);
        }
        "json" => {
            // Parse YAML and convert to JSON
            let value: serde_yaml::Value = serde_yaml::from_str(schema)?;
            let json = serde_json::to_string_pretty(&value)?;
            println!("{}", json);
        }
        "markdown" | "md" => {
            print_schema_markdown();
        }
        _ => {
            anyhow::bail!("Unknown format: {}. Use yaml, json, or markdown.", format);
        }
    }

    Ok(())
}

/// Print schema as markdown documentation
fn print_schema_markdown() {
    println!(
        r#"# gitgrip Manifest Schema (v2)

## Overview

The workspace file (`gripspace.yml`) defines a multi-repository workspace configuration.
It is typically located at `.gitgrip/spaces/main/gripspace.yml`.

## Top-Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | integer | Yes | Schema version (currently `2`) |
| `remotes` | object | No | Named remotes with base fetch URLs |
| `gripspaces` | array | No | Gripspace includes for manifest inheritance |
| `manifest` | object | No | Self-tracking manifest repo config |
| `repos` | object | Yes | Repository definitions |
| `settings` | object | No | Global workspace settings |
| `workspace` | object | No | Scripts, hooks, and environment |

## Remotes

Named remotes with base fetch URLs. Repos can reference a remote by name
instead of specifying a full URL. The repo name + `.git` is auto-appended.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `fetch` | string | Yes | Base fetch URL (e.g. `git@github.com:org/`) |

## Gripspace Includes

Include repos, scripts, env, hooks, and linkfiles from other gripspace repos.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | Git URL for the gripspace repository |
| `rev` | string | No | Branch, tag, or commit to pin (default: remote HEAD) |

### Composefile (in manifest section)

Generate files by concatenating parts from gripspaces and local manifest.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `dest` | string | Yes | Destination path relative to workspace root |
| `separator` | string | No | Separator between parts (default: `\n\n`) |
| `parts` | array | Yes | Ordered list of content sources |
| `parts[].src` | string | Yes | Source file path |
| `parts[].gripspace` | string | No | Gripspace name (omit for local manifest) |

## Repository Configuration

Each repository under `repos` supports:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | - | Git URL (SSH or HTTPS). Required unless `remote` is set. |
| `remote` | string | - | Reference to a top-level remote (derives URL from remote base + repo name) |
| `path` | string | - | Local path relative to workspace |
| `revision` | string | `main` | Default revision/branch to clone (inherits from settings) |
| `target` | string | revision | PR base branch name (inherits from settings, then revision) |
| `sync_remote` | string | `origin` | Remote for fetch/rebase (inherits from settings) |
| `push_remote` | string | `origin` | Remote for push (inherits from settings) |
| `groups` | array | `[]` | Groups for selective operations |
| `reference` | boolean | `false` | Read-only reference repo |
| `copyfile` | array | - | Files to copy to workspace |
| `linkfile` | array | - | Symlinks to create |
| `platform` | object | auto | Platform type and base URL |
| `agent` | object | - | Agent context (AI tool metadata) |
| `agent.description` | string | - | What this repo does |
| `agent.language` | string | - | Primary language |
| `agent.build` | string | - | Build command |
| `agent.test` | string | - | Test command |
| `agent.lint` | string | - | Lint command |
| `agent.format` | string | - | Format command |

### Resolution Chains

- **`revision`**: repo > settings > `"main"`
- **`target`**: repo > settings > revision
- **`sync_remote`**: repo > settings > `"origin"`
- **`push_remote`**: repo > settings > `"origin"`
- **`url`**: explicit `url`, or `remotes[repo.remote].fetch + name + ".git"`

## Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pr_prefix` | string | `[cross-repo]` | Prefix for PR titles |
| `merge_strategy` | string | `all-or-nothing` | `all-or-nothing` or `independent` |
| `revision` | string | `main` | Default revision for all repos |
| `target` | string | revision | PR base branch for all repos |
| `sync_remote` | string | `origin` | Default remote for fetch/rebase |
| `push_remote` | string | `origin` | Default remote for push |

## Platform Types

- `github` - GitHub.com or GitHub Enterprise
- `gitlab` - GitLab.com or self-hosted
- `azure-devops` - Azure DevOps or Azure DevOps Server
- `bitbucket` - Bitbucket Cloud or Server

## Workspace Agent Config

| Field | Type | Description |
|-------|------|-------------|
| `agent.description` | string | Workspace description for agents |
| `agent.conventions` | array | Coding conventions to follow |
| `agent.workflows` | object | Named workflow descriptions |

## Example

```yaml
version: 2

remotes:
  upstream:
    fetch: git@github.com:org/

gripspaces:
  - url: https://github.com/org/base-gripspace.git
    rev: main

manifest:
  url: git@github.com:org/manifest.git
  revision: main
  composefile:
    - dest: CLAUDE.md
      parts:
        - gripspace: base-gripspace
          src: CODI.md
        - src: PRIVATE_DOCS.md

repos:
  frontend:
    url: git@github.com:me/frontend.git
    path: ./frontend
    sync_remote: upstream
    groups: [core, web]
    agent:
      description: "React web application"
      language: typescript
      build: "pnpm build"
      test: "pnpm test"
      lint: "pnpm lint"

  backend:
    remote: upstream
    path: ./backend
    revision: master
    target: staging
    groups: [core, api]
    agent:
      description: "Rust API server"
      language: rust
      build: "cargo build"
      test: "cargo test"
      lint: "cargo clippy"
      format: "cargo fmt"

settings:
  pr_prefix: "[multi-repo]"
  merge_strategy: all-or-nothing
  revision: main
  target: develop

workspace:
  agent:
    description: "Multi-repo web application"
    conventions:
      - "Use conventional commits"
      - "All PRs require review"
    workflows:
      deploy: "gr pr merge && ./scripts/deploy.sh"
  scripts:
    build:
      command: "npm run build"
```

## v1 Backward Compatibility

v1 manifests are auto-migrated when parsed:
- `default_branch` is accepted as an alias for `revision`
- `target` containing "/" (e.g. `upstream/develop`) is split into `target=develop`, `sync_remote=upstream`
- Version is upgraded to 2 internally
"#
    );
}

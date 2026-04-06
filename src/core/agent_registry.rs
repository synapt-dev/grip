//! Org Agent Registry — stable agent identity for channel scoping (#510).
//!
//! Each agent gets a unique, permanent ID within their org. IDs are stored
//! in `~/.synapt/orgs/<org_id>/team.db` and passed to agent sessions via
//! the `SYNAPT_AGENT_ID` environment variable.
//!
//! Phase 0 of the channel scoping design (config/design/channel-scoping.md).

use std::path::{Path, PathBuf};

use anyhow::Result;

/// An agent's identity within an org.
#[derive(Debug, Clone)]
pub struct AgentEntry {
    pub agent_id: String,
    pub display_name: String,
    pub role: Option<String>,
    pub org_id: String,
}

/// Register a new agent in the org registry.
///
/// Returns the assigned `agent_id`. If an agent with the same
/// `display_name` already exists in the org, returns an error.
pub fn register_agent(
    _org_dir: &Path,
    _org_id: &str,
    _display_name: &str,
    _role: Option<&str>,
) -> Result<String> {
    // TODO: implement — this stub exists so tests can compile but fail
    unimplemented!("register_agent not yet implemented")
}

/// Look up an agent by ID.
pub fn get_agent(_org_dir: &Path, _agent_id: &str) -> Result<Option<AgentEntry>> {
    unimplemented!("get_agent not yet implemented")
}

/// Look up an agent by display name within an org.
pub fn get_agent_by_name(
    _org_dir: &Path,
    _org_id: &str,
    _display_name: &str,
) -> Result<Option<AgentEntry>> {
    unimplemented!("get_agent_by_name not yet implemented")
}

/// List all agents in an org.
pub fn list_agents(_org_dir: &Path, _org_id: &str) -> Result<Vec<AgentEntry>> {
    unimplemented!("list_agents not yet implemented")
}

/// Update an agent's display name (agent_id stays the same).
pub fn rename_agent(_org_dir: &Path, _agent_id: &str, _new_display_name: &str) -> Result<()> {
    unimplemented!("rename_agent not yet implemented")
}

/// Return the org directory path: `~/.synapt/orgs/<org_id>/`
pub fn org_dir(org_id: &str) -> PathBuf {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join(".synapt")
        .join("orgs")
        .join(org_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn setup_org_dir() -> (TempDir, PathBuf) {
        let tmp = TempDir::new().unwrap();
        let org_path = tmp.path().join("synapt-dev");
        fs::create_dir_all(&org_path).unwrap();
        (tmp, org_path)
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_register_agent_assigns_id() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", Some("implementation")).unwrap();
        assert!(!id.is_empty());
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_agent_id_is_stable() {
        let (_tmp, org_path) = setup_org_dir();
        let id1 = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        // Looking up by name should return the same ID
        let entry = get_agent_by_name(&org_path, "synapt-dev", "Apollo")
            .unwrap()
            .unwrap();
        assert_eq!(entry.agent_id, id1);
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_duplicate_display_name_rejected() {
        let (_tmp, org_path) = setup_org_dir();
        register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        // Second registration with same name should fail
        let result = register_agent(&org_path, "synapt-dev", "Apollo", None);
        assert!(result.is_err());
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_agent_ids_are_unique() {
        let (_tmp, org_path) = setup_org_dir();
        let id1 = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        let id2 = register_agent(&org_path, "synapt-dev", "Atlas", None).unwrap();
        assert_ne!(id1, id2);
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_agent_id_format() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        // ID should be lowercase name + dash + number (e.g. "apollo-001")
        assert!(id.starts_with("apollo-"), "Expected apollo-NNN, got {}", id);
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_list_agents_returns_all() {
        let (_tmp, org_path) = setup_org_dir();
        register_agent(&org_path, "synapt-dev", "Apollo", Some("impl")).unwrap();
        register_agent(&org_path, "synapt-dev", "Atlas", Some("research")).unwrap();
        register_agent(&org_path, "synapt-dev", "Sentinel", Some("ops")).unwrap();
        let agents = list_agents(&org_path, "synapt-dev").unwrap();
        assert_eq!(agents.len(), 3);
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_rename_preserves_agent_id() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        rename_agent(&org_path, &id, "ApolloV2").unwrap();
        let entry = get_agent(&org_path, &id).unwrap().unwrap();
        assert_eq!(entry.agent_id, id);
        assert_eq!(entry.display_name, "ApolloV2");
    }

    #[test]
    #[should_panic(expected = "not yet implemented")]
    fn test_get_agent_by_id() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", Some("implementation")).unwrap();
        let entry = get_agent(&org_path, &id).unwrap().unwrap();
        assert_eq!(entry.display_name, "Apollo");
        assert_eq!(entry.role, Some("implementation".to_string()));
        assert_eq!(entry.org_id, "synapt-dev");
    }
}

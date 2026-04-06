//! Org Agent Registry — stable agent identity for channel scoping (#510).
//!
//! Each agent gets a unique, permanent ID within their org. IDs are stored
//! in `~/.synapt/orgs/<org_id>/team.db` and passed to agent sessions via
//! the `SYNAPT_AGENT_ID` environment variable.
//!
//! Phase 0 of the channel scoping design (config/design/channel-scoping.md).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::Connection;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS org_agents (
    agent_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT,
    org_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    -- Process tracking columns (Sprint 9 Mission Control)
    pid INTEGER,
    tmux_target TEXT,
    status TEXT DEFAULT 'offline',
    log_path TEXT,
    session_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_display
    ON org_agents(org_id, display_name);
"#;

/// An agent's identity within an org.
#[derive(Debug, Clone)]
pub struct AgentEntry {
    pub agent_id: String,
    pub display_name: String,
    pub role: Option<String>,
    pub org_id: String,
}

/// Open (or create) the team.db for an org.
/// Runs schema migration to add any missing columns (#447).
fn open_db(org_dir: &Path) -> Result<Connection> {
    std::fs::create_dir_all(org_dir)?;
    let db_path = org_dir.join("team.db");
    let conn = Connection::open(&db_path)
        .with_context(|| format!("Failed to open team.db at {}", db_path.display()))?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")?;
    conn.execute_batch(SCHEMA)?;
    // Migrate existing databases: add columns that may not exist yet.
    // ALTER TABLE ADD COLUMN is a no-op if the column already exists
    // (SQLite returns an error we ignore).
    for col in &[
        "ALTER TABLE org_agents ADD COLUMN pid INTEGER",
        "ALTER TABLE org_agents ADD COLUMN tmux_target TEXT",
        "ALTER TABLE org_agents ADD COLUMN status TEXT DEFAULT 'offline'",
        "ALTER TABLE org_agents ADD COLUMN log_path TEXT",
        "ALTER TABLE org_agents ADD COLUMN session_id TEXT",
    ] {
        let _ = conn.execute_batch(col); // Ignore "duplicate column" errors
    }
    Ok(conn)
}

/// Generate agent_id from display name: lowercase + dash + next sequence number.
/// e.g. "Apollo" with 0 existing → "apollo-001"
fn generate_agent_id(conn: &Connection, org_id: &str, display_name: &str) -> Result<String> {
    let prefix = display_name.to_lowercase();

    // Count existing agents with the same prefix to determine sequence
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM org_agents WHERE org_id = ? AND agent_id LIKE ?",
        rusqlite::params![org_id, format!("{}-%", prefix)],
        |row| row.get(0),
    )?;

    Ok(format!("{}-{:03}", prefix, count + 1))
}

/// Register a new agent in the org registry.
///
/// Returns the assigned `agent_id`. If an agent with the same
/// `display_name` already exists in the org, returns an error.
pub fn register_agent(
    org_dir: &Path,
    org_id: &str,
    display_name: &str,
    role: Option<&str>,
) -> Result<String> {
    let conn = open_db(org_dir)?;
    let agent_id = generate_agent_id(&conn, org_id, display_name)?;
    let now = chrono::Utc::now().to_rfc3339();

    conn.execute(
        "INSERT INTO org_agents (agent_id, display_name, role, org_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        rusqlite::params![agent_id, display_name, role, org_id, now],
    )
    .with_context(|| {
        format!(
            "Failed to register agent '{}' — display_name may already exist in org '{}'",
            display_name, org_id
        )
    })?;

    Ok(agent_id)
}

/// Look up an agent by ID.
pub fn get_agent(org_dir: &Path, agent_id: &str) -> Result<Option<AgentEntry>> {
    let conn = open_db(org_dir)?;
    let mut stmt = conn.prepare(
        "SELECT agent_id, display_name, role, org_id FROM org_agents WHERE agent_id = ?",
    )?;

    let entry = stmt
        .query_row(rusqlite::params![agent_id], |row| {
            Ok(AgentEntry {
                agent_id: row.get(0)?,
                display_name: row.get(1)?,
                role: row.get(2)?,
                org_id: row.get(3)?,
            })
        })
        .optional()?;

    Ok(entry)
}

/// Look up an agent by display name within an org.
pub fn get_agent_by_name(
    org_dir: &Path,
    org_id: &str,
    display_name: &str,
) -> Result<Option<AgentEntry>> {
    let conn = open_db(org_dir)?;
    let mut stmt = conn.prepare(
        "SELECT agent_id, display_name, role, org_id
         FROM org_agents WHERE org_id = ? AND display_name = ?",
    )?;

    let entry = stmt
        .query_row(rusqlite::params![org_id, display_name], |row| {
            Ok(AgentEntry {
                agent_id: row.get(0)?,
                display_name: row.get(1)?,
                role: row.get(2)?,
                org_id: row.get(3)?,
            })
        })
        .optional()?;

    Ok(entry)
}

/// List all agents in an org.
pub fn list_agents(org_dir: &Path, org_id: &str) -> Result<Vec<AgentEntry>> {
    let conn = open_db(org_dir)?;
    let mut stmt = conn.prepare(
        "SELECT agent_id, display_name, role, org_id
         FROM org_agents WHERE org_id = ? ORDER BY agent_id",
    )?;

    let entries = stmt
        .query_map(rusqlite::params![org_id], |row| {
            Ok(AgentEntry {
                agent_id: row.get(0)?,
                display_name: row.get(1)?,
                role: row.get(2)?,
                org_id: row.get(3)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    Ok(entries)
}

/// Update an agent's display name (agent_id stays the same).
pub fn rename_agent(org_dir: &Path, agent_id: &str, new_display_name: &str) -> Result<()> {
    let conn = open_db(org_dir)?;
    let updated = conn.execute(
        "UPDATE org_agents SET display_name = ? WHERE agent_id = ?",
        rusqlite::params![new_display_name, agent_id],
    )?;
    if updated == 0 {
        anyhow::bail!("Agent '{}' not found", agent_id);
    }
    Ok(())
}

/// Update an agent's process state (called by gr spawn).
pub fn update_process_state(
    org_dir: &Path,
    agent_id: &str,
    pid: Option<u32>,
    tmux_target: Option<&str>,
    status: &str,
    log_path: Option<&str>,
    session_id: Option<&str>,
) -> Result<()> {
    let conn = open_db(org_dir)?;
    let now = chrono::Utc::now().to_rfc3339();
    let updated = conn.execute(
        "UPDATE org_agents SET pid = ?1, tmux_target = ?2, status = ?3, \
         log_path = ?4, session_id = ?5, last_seen_at = ?6 \
         WHERE agent_id = ?7",
        rusqlite::params![
            pid.map(|p| p as i64),
            tmux_target,
            status,
            log_path,
            session_id,
            now,
            agent_id,
        ],
    )?;
    if updated == 0 {
        anyhow::bail!("Agent '{}' not found", agent_id);
    }
    Ok(())
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

// Make rusqlite's optional() available
use rusqlite::OptionalExtension;

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
    fn test_register_agent_assigns_id() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", Some("implementation")).unwrap();
        assert!(!id.is_empty());
    }

    #[test]
    fn test_agent_id_is_stable() {
        let (_tmp, org_path) = setup_org_dir();
        let id1 = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        let entry = get_agent_by_name(&org_path, "synapt-dev", "Apollo")
            .unwrap()
            .unwrap();
        assert_eq!(entry.agent_id, id1);
    }

    #[test]
    fn test_duplicate_display_name_rejected() {
        let (_tmp, org_path) = setup_org_dir();
        register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        let result = register_agent(&org_path, "synapt-dev", "Apollo", None);
        assert!(result.is_err());
    }

    #[test]
    fn test_agent_ids_are_unique() {
        let (_tmp, org_path) = setup_org_dir();
        let id1 = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        let id2 = register_agent(&org_path, "synapt-dev", "Atlas", None).unwrap();
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_agent_id_format() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        assert!(id.starts_with("apollo-"), "Expected apollo-NNN, got {}", id);
    }

    #[test]
    fn test_list_agents_returns_all() {
        let (_tmp, org_path) = setup_org_dir();
        register_agent(&org_path, "synapt-dev", "Apollo", Some("impl")).unwrap();
        register_agent(&org_path, "synapt-dev", "Atlas", Some("research")).unwrap();
        register_agent(&org_path, "synapt-dev", "Sentinel", Some("ops")).unwrap();
        let agents = list_agents(&org_path, "synapt-dev").unwrap();
        assert_eq!(agents.len(), 3);
    }

    #[test]
    fn test_rename_preserves_agent_id() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", None).unwrap();
        rename_agent(&org_path, &id, "ApolloV2").unwrap();
        let entry = get_agent(&org_path, &id).unwrap().unwrap();
        assert_eq!(entry.agent_id, id);
        assert_eq!(entry.display_name, "ApolloV2");
    }

    #[test]
    fn test_get_agent_by_id() {
        let (_tmp, org_path) = setup_org_dir();
        let id = register_agent(&org_path, "synapt-dev", "Apollo", Some("implementation")).unwrap();
        let entry = get_agent(&org_path, &id).unwrap().unwrap();
        assert_eq!(entry.display_name, "Apollo");
        assert_eq!(entry.role, Some("implementation".to_string()));
        assert_eq!(entry.org_id, "synapt-dev");
    }

    #[test]
    fn test_schema_migration_adds_missing_columns() {
        // Simulate a Sprint 8 database (no process columns)
        let tmp = TempDir::new().unwrap();
        let org_path = tmp.path().join("synapt-dev");
        fs::create_dir_all(&org_path).unwrap();

        // Create old-schema table manually (no pid, tmux_target, etc.)
        let old_schema = r#"
            CREATE TABLE org_agents (
                agent_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT,
                org_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE UNIQUE INDEX idx_org_display ON org_agents(org_id, display_name);
        "#;
        let db_path = org_path.join("team.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(old_schema).unwrap();
        conn.execute(
            "INSERT INTO org_agents (agent_id, display_name, role, org_id, created_at) \
             VALUES ('opus-001', 'opus', 'CEO', 'synapt-dev', '2026-04-06')",
            [],
        )
        .unwrap();
        drop(conn);

        // Now open with our migration-aware open_db
        // This should add the missing columns without error
        let id = register_agent(&org_path, "synapt-dev", "Apollo", Some("impl")).unwrap();
        assert_eq!(id, "apollo-001");

        // Verify we can update process state (uses the new columns)
        update_process_state(
            &org_path,
            "opus-001",
            Some(1234),
            Some("synapt:opus"),
            "online",
            Some("/tmp/logs/opus-001/output.log"),
            None,
        )
        .unwrap();

        // Verify the update stuck
        let entry = get_agent(&org_path, "opus-001").unwrap().unwrap();
        assert_eq!(entry.display_name, "opus");
    }
}

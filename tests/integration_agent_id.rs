//! Integration test: agent registry → spawn → SYNAPT_AGENT_ID pipeline.
//!
//! Verifies that gr spawn creates registry entries in team.db and
//! that the generated agent_id follows the name-NNN format.
//! The full end-to-end (Rust → Python channel) is tested via the
//! Conversa migration test in CI.

use std::fs;
use std::path::PathBuf;
use tempfile::TempDir;

// Re-export the registry functions we're testing
use gitgrip::core::agent_registry::{
    get_agent, get_agent_by_name, list_agents, register_agent, rename_agent,
};

/// Integration test: register 4 agents (matching Conversa team),
/// verify all get unique stable IDs, then simulate a restart
/// (re-lookup) and confirm IDs are preserved.
#[test]
fn test_full_agent_lifecycle() {
    let tmp = TempDir::new().unwrap();
    let org_dir = tmp.path().join("conversa");
    fs::create_dir_all(&org_dir).unwrap();

    let org_id = "conversa";

    // Phase 1: Register 4 agents (simulates gr spawn up)
    let anchor_id = register_agent(&org_dir, org_id, "anchor", Some("CEO — coordination")).unwrap();
    let opus_id = register_agent(&org_dir, org_id, "opus", Some("CTO — data backbone")).unwrap();
    let atlas_id = register_agent(&org_dir, org_id, "atlas", Some("CXO — UI, design")).unwrap();
    let forge_id = register_agent(&org_dir, org_id, "forge", Some("Chief Architect")).unwrap();

    // Verify format: name-NNN
    assert_eq!(anchor_id, "anchor-001");
    assert_eq!(opus_id, "opus-001");
    assert_eq!(atlas_id, "atlas-001");
    assert_eq!(forge_id, "forge-001");

    // Phase 2: Simulate restart — lookup by name (same as spawn up does)
    let anchor_lookup = get_agent_by_name(&org_dir, org_id, "anchor")
        .unwrap()
        .unwrap();
    assert_eq!(anchor_lookup.agent_id, "anchor-001");
    assert_eq!(anchor_lookup.role, Some("CEO — coordination".to_string()));

    // Phase 3: List all agents
    let all = list_agents(&org_dir, org_id).unwrap();
    assert_eq!(all.len(), 4);

    // Phase 4: Rename doesn't change ID
    rename_agent(&org_dir, "opus-001", "Opus Prime").unwrap();
    let renamed = get_agent(&org_dir, "opus-001").unwrap().unwrap();
    assert_eq!(renamed.agent_id, "opus-001"); // ID unchanged
    assert_eq!(renamed.display_name, "Opus Prime"); // Name updated

    // Phase 5: Duplicate registration fails
    let dup = register_agent(&org_dir, org_id, "anchor", None);
    assert!(dup.is_err(), "Duplicate display_name should be rejected");

    // Phase 6: IDs are unique
    let ids: Vec<String> = all.iter().map(|a| a.agent_id.clone()).collect();
    let unique: std::collections::HashSet<_> = ids.iter().collect();
    assert_eq!(ids.len(), unique.len(), "All IDs must be unique");
}

/// Integration test: cross-org isolation — same name in different orgs
/// gets separate IDs with no collision.
#[test]
fn test_cross_org_isolation() {
    let tmp = TempDir::new().unwrap();
    let synapt_dir = tmp.path().join("synapt-dev");
    let conversa_dir = tmp.path().join("conversa");
    fs::create_dir_all(&synapt_dir).unwrap();
    fs::create_dir_all(&conversa_dir).unwrap();

    // Same display name, different orgs
    let synapt_atlas =
        register_agent(&synapt_dir, "synapt-dev", "Atlas", Some("research")).unwrap();
    let conversa_atlas =
        register_agent(&conversa_dir, "conversa", "Atlas", Some("design")).unwrap();

    // Both succeed — no cross-org collision
    assert_eq!(synapt_atlas, "atlas-001");
    assert_eq!(conversa_atlas, "atlas-001");

    // Each org has only 1 agent
    assert_eq!(list_agents(&synapt_dir, "synapt-dev").unwrap().len(), 1);
    assert_eq!(list_agents(&conversa_dir, "conversa").unwrap().len(), 1);

    // Roles are org-specific
    let s = get_agent(&synapt_dir, "atlas-001").unwrap().unwrap();
    let c = get_agent(&conversa_dir, "atlas-001").unwrap().unwrap();
    assert_eq!(s.role, Some("research".to_string()));
    assert_eq!(c.role, Some("design".to_string()));
}

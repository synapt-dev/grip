use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use gr2_cli::overlay::{
    apply_tier_a_overlay, capture_tier_a_overlay, CapturedOverlay, OverlayCaptureRequest,
    OverlayMetadata,
};
use tempfile::TempDir;

#[test]
fn capture_tier_a_overlay_writes_annotated_tag_with_structured_tree_and_metadata() {
    let temp = TempDir::new().unwrap();
    let overlay_store = init_bare_git_repo(&temp, "overlay-store.git");
    let source_root = temp.path().join("overlay-source");
    fs::create_dir_all(&source_root).unwrap();

    write_file(&source_root, "COMPOSE.md", "overlay compose\n");
    write_file(&source_root, "agents.toml", "name = \"atlas\"\n");
    write_file(&source_root, "pipelines/app.yml", "name: ci\n");
    write_file(&source_root, "prompts/task.json", "{\n  \"name\": \"review\"\n}\n");
    write_file(&source_root, "ignored.py", "print('not tier a')\n");

    let captured = capture_tier_a_overlay(OverlayCaptureRequest {
        overlay_store: overlay_store.clone(),
        source_root: source_root.clone(),
        overlay_name: "team/config-base".to_string(),
        metadata: OverlayMetadata {
            author: "atlas".to_string(),
            signature: "unsigned".to_string(),
            timestamp: "2026-05-01T00:00:00Z".to_string(),
            parent_overlay_refs: vec![
                "refs/overlays/team/shared-base".to_string(),
                "refs/overlays/atlas/personal-tweaks".to_string(),
            ],
        },
    })
    .expect("Tier A overlay capture should succeed");

    assert_annotated_tag_points_to_structured_tree(&overlay_store, &captured);

    let structured_entries = ls_tree(&overlay_store, &captured.structured_tree_oid);
    assert_eq!(
        structured_entries,
        vec![
            "metadata_blob".to_string(),
            "staged_index_tree".to_string(),
            "untracked_blobs".to_string(),
            "working_tree_tree".to_string(),
        ]
    );

    let metadata_text = cat_blob_from_tree(&overlay_store, &captured.structured_tree_oid, "metadata_blob");
    assert!(metadata_text.contains("author = \"atlas\""));
    assert!(metadata_text.contains("signature = \"unsigned\""));
    assert!(metadata_text.contains("timestamp = \"2026-05-01T00:00:00Z\""));
    assert!(metadata_text.contains("refs/overlays/team/shared-base"));
    assert!(metadata_text.contains("refs/overlays/atlas/personal-tweaks"));

    let working_tree_oid = tree_entry_oid(&overlay_store, &captured.structured_tree_oid, "working_tree_tree");
    let staged_tree_oid = tree_entry_oid(&overlay_store, &captured.structured_tree_oid, "staged_index_tree");

    let expected_files = vec![
        "COMPOSE.md".to_string(),
        "agents.toml".to_string(),
        "pipelines/app.yml".to_string(),
        "prompts/task.json".to_string(),
    ];
    assert_eq!(flatten_tree(&overlay_store, &working_tree_oid), expected_files);
    assert_eq!(flatten_tree(&overlay_store, &staged_tree_oid), flatten_tree(&overlay_store, &working_tree_oid));
    assert!(!flatten_tree(&overlay_store, &working_tree_oid).contains(&"ignored.py".to_string()));
}

#[test]
fn apply_tier_a_overlay_round_trips_and_is_idempotent() {
    let temp = TempDir::new().unwrap();
    let overlay_store = init_bare_git_repo(&temp, "overlay-store.git");
    let source_root = temp.path().join("overlay-source");
    let target_root = temp.path().join("clean-checkout");
    fs::create_dir_all(&source_root).unwrap();
    fs::create_dir_all(&target_root).unwrap();

    write_file(&source_root, "COMPOSE.md", "overlay compose\n");
    write_file(&source_root, "settings.toml", "theme = \"owl\"\n");
    write_file(&source_root, "skills/ci.yml", "steps:\n  - lint\n");
    write_file(&source_root, "prompts/review.json", "{\n  \"prompt\": \"be precise\"\n}\n");
    write_file(&source_root, "ignored.rs", "fn main() {}\n");

    let captured = capture_tier_a_overlay(OverlayCaptureRequest {
        overlay_store: overlay_store.clone(),
        source_root: source_root.clone(),
        overlay_name: "atlas/review-defaults".to_string(),
        metadata: OverlayMetadata {
            author: "atlas".to_string(),
            signature: "unsigned".to_string(),
            timestamp: "2026-05-01T00:00:00Z".to_string(),
            parent_overlay_refs: Vec::new(),
        },
    })
    .expect("Tier A overlay capture should succeed");

    apply_tier_a_overlay(&overlay_store, &captured.tag_ref, &target_root)
        .expect("first apply should succeed");
    let first_snapshot = snapshot_files(&target_root);

    assert_eq!(
        first_snapshot,
        BTreeMap::from([
            ("COMPOSE.md".to_string(), "overlay compose\n".to_string()),
            ("prompts/review.json".to_string(), "{\n  \"prompt\": \"be precise\"\n}\n".to_string()),
            ("settings.toml".to_string(), "theme = \"owl\"\n".to_string()),
            ("skills/ci.yml".to_string(), "steps:\n  - lint\n".to_string()),
        ])
    );
    assert!(!target_root.join("ignored.rs").exists());

    apply_tier_a_overlay(&overlay_store, &captured.tag_ref, &target_root)
        .expect("second apply should also succeed");
    let second_snapshot = snapshot_files(&target_root);

    assert_eq!(second_snapshot, first_snapshot);
}

fn init_bare_git_repo(temp: &TempDir, name: &str) -> PathBuf {
    let repo_path = temp.path().join(name);
    fs::create_dir_all(&repo_path).unwrap();
    let output = Command::new("git")
        .args(["init", "--bare"])
        .current_dir(&repo_path)
        .output()
        .expect("failed to init bare repo");
    assert!(
        output.status.success(),
        "git init --bare failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    repo_path
}

fn write_file(root: &Path, relative: &str, contents: &str) {
    let path = root.join(relative);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, contents).unwrap();
}

fn assert_annotated_tag_points_to_structured_tree(overlay_store: &Path, captured: &CapturedOverlay) {
    let tag_type = git_output(overlay_store, &["cat-file", "-t", &captured.tag_oid]);
    assert_eq!(tag_type, "tag");

    let tag_body = git_output(overlay_store, &["cat-file", "-p", &captured.tag_oid]);
    assert!(tag_body.contains("type tree"));
    assert!(tag_body.contains("tag "));

    let peeled_tree = git_output(overlay_store, &["rev-parse", &format!("{}^{{tree}}", captured.tag_oid)]);
    assert_eq!(peeled_tree, captured.structured_tree_oid);
}

fn ls_tree(overlay_store: &Path, tree_oid: &str) -> Vec<String> {
    let output = git_output(overlay_store, &["ls-tree", "--name-only", tree_oid]);
    let mut entries = output.lines().map(str::to_string).collect::<Vec<_>>();
    entries.sort();
    entries
}

fn tree_entry_oid(overlay_store: &Path, tree_oid: &str, entry_name: &str) -> String {
    let output = git_output(overlay_store, &["ls-tree", tree_oid, entry_name]);
    output
        .split_whitespace()
        .nth(2)
        .expect("tree entry oid missing")
        .to_string()
}

fn cat_blob_from_tree(overlay_store: &Path, tree_oid: &str, entry_name: &str) -> String {
    let spec = format!("{tree_oid}:{entry_name}");
    git_output(overlay_store, &["show", &spec])
}

fn flatten_tree(overlay_store: &Path, tree_oid: &str) -> Vec<String> {
    let output = git_output(overlay_store, &["ls-tree", "-r", "--name-only", tree_oid]);
    let mut files = output.lines().map(str::to_string).collect::<Vec<_>>();
    files.sort();
    files
}

fn snapshot_files(root: &Path) -> BTreeMap<String, String> {
    let mut snapshot = BTreeMap::new();
    collect_snapshot(root, root, &mut snapshot);
    snapshot
}

fn collect_snapshot(root: &Path, current: &Path, snapshot: &mut BTreeMap<String, String>) {
    let mut entries = fs::read_dir(current)
        .unwrap()
        .map(|entry| entry.unwrap())
        .collect::<Vec<_>>();
    entries.sort_by_key(|entry| entry.path());

    for entry in entries {
        let path = entry.path();
        if entry.file_type().unwrap().is_dir() {
            collect_snapshot(root, &path, snapshot);
        } else {
            let relative = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            let contents = fs::read_to_string(&path).unwrap();
            snapshot.insert(relative, contents);
        }
    }
}

fn git_output(git_dir: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg(format!("--git-dir={}", git_dir.display()))
        .args(args)
        .output()
        .unwrap_or_else(|err| panic!("failed to run git {:?}: {}", args, err));
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

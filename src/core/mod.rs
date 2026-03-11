//! Core business logic for gitgrip

pub mod detect;
pub mod gripspace;
pub mod griptree;
pub mod manifest;
pub mod manifest_paths;
pub mod repo;
pub mod repo_manifest;
pub mod state;
pub mod sync_state;

pub use manifest::Manifest;
pub use repo::RepoInfo;
pub use state::StateFile;

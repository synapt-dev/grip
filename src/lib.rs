//! gitgrip - Multi-repo workflow tool
//!
//! A high-performance tool for managing multi-repository workspaces,
//! with support for GitHub, GitLab, and Azure DevOps.

pub mod cli;
pub mod core;
pub mod files;
pub mod git;
pub mod gr2;
pub mod ipc;
pub mod mcp;
pub mod platform;
pub mod telemetry;
pub mod util;

pub use core::manifest::Manifest;
pub use core::repo::RepoInfo;
pub use core::state::StateFile;

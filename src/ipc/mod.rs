//! IPC module for agent wake coordination.
//!
//! Ported from codi-rs `src/orchestrate/ipc/` and adapted for gitgrip's
//! WakeCoordinator pattern. Provides cross-platform IPC between the gripspace
//! daemon (or `gr spawn` event loop) and agent processes.
//!
//! # Protocol
//!
//! Messages are newline-delimited JSON (NDJSON). Transport is Unix domain
//! sockets on Unix, named pipes on Windows.

pub mod error;
pub mod protocol;
pub mod server;
pub mod transport;

pub use error::{IpcError, IpcResult};
pub use protocol::{AgentMessage, CoordinatorMessage, WakePriority, WakeReason};
pub use server::{IpcServer, ServerEvent};

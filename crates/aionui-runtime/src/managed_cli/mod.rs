//! Resolution of bundled claude CLI binary.
//!
//! Claude runs through the direct-CLI session path. In a packaged app the
//! CLI is shipped inside `managed-resources/cli/claude/<version>/<target>/`
//! and this module resolves the running platform's main executable there. In
//! dev / non-bundled runs it returns `None` so the caller falls back to the
//! bare command name (resolved via PATH). This resolution is intentionally
//! separate from `cli_probe` (availability detection stays PATH-only).

use std::path::{Path, PathBuf};

use crate::managed_resources::{self, ManagedResourcesMode};

mod prepare;
pub use prepare::{ManagedCliError, PreparedCli, managed_cli_contract_for_export, prepare_managed_cli_to_root};

/// Pinned Claude CLI version — bump and rebuild to update.
pub const CLAUDE_CLI_VERSION: &str = "2.1.215";

/// The pinned version for a supported CLI name, or `None` for unknown names.
pub fn cli_version(name: &str) -> Option<&'static str> {
    match name {
        "claude" => Some(CLAUDE_CLI_VERSION),
        _ => None,
    }
}

/// The runtime key (`<os>-<arch>`) identifying the current platform's bundled
/// CLI subtree, mirroring node's `platform_spec` runtime_key values.
pub fn current_runtime_key() -> Option<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Some("darwin-arm64"),
        ("macos", "x86_64") => Some("darwin-x64"),
        ("linux", "aarch64") => Some("linux-arm64"),
        ("linux", "x86_64") => Some("linux-x64"),
        ("windows", "x86_64") => Some("win32-x64"),
        ("windows", "aarch64") => Some("win32-arm64"),
        _ => None,
    }
}

fn exe_suffix() -> &'static str {
    if cfg!(windows) { ".exe" } else { "" }
}

/// Locate the main executable inside a materialized CLI root.
/// Claude ships as a single `claude[.exe]` at the root.
fn main_executable_in(name: &str, root: &Path) -> Option<PathBuf> {
    match name {
        "claude" => {
            let candidate = root.join(format!("claude{}", exe_suffix()));
            candidate.is_file().then_some(candidate)
        }
        _ => None,
    }
}

/// Resolve the absolute path to the bundled CLI binary for `name`.
///
/// Returns `Some(path)` only in `Bundled` mode when the binary is present;
/// otherwise `None` (dev / Download mode, or a missing bundle), signalling the
/// caller to fall back to the bare command name resolved via PATH.
pub fn resolve_bundled_cli(name: &str) -> Option<PathBuf> {
    if !matches!(
        managed_resources::managed_resources_mode(),
        ManagedResourcesMode::Bundled
    ) {
        return None;
    }
    let version = cli_version(name)?;
    let target = current_runtime_key()?;
    for source in managed_resources::cli_sources(name, version, target) {
        if let Some(exe) = main_executable_in(name, &source.root) {
            tracing::info!(cli = name, version, path = %exe.display(), "resolved bundled cli");
            return Some(exe);
        }
    }
    let resolved = crate::resolve_command_path(name)
        .map(|path| path.display().to_string())
        .unwrap_or_else(|| "<none>".to_owned());
    tracing::warn!(
        cli = name,
        version,
        resolved = %resolved,
        "bundled cli missing in Bundled mode; falling back to PATH"
    );
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_version_returns_pins() {
        assert_eq!(cli_version("claude"), Some(CLAUDE_CLI_VERSION));
        assert_eq!(cli_version("gemini"), None);
    }

    #[test]
    fn current_runtime_key_is_supported_on_this_host() {
        assert!(current_runtime_key().is_some());
    }

    #[test]
    fn main_executable_finds_claude_single_file() {
        let dir = tempfile::tempdir().expect("tempdir");
        let exe = dir.path().join(format!("claude{}", exe_suffix()));
        std::fs::write(&exe, b"#!/bin/sh\n").expect("write");
        assert_eq!(main_executable_in("claude", dir.path()), Some(exe));
    }

    #[test]
    fn main_executable_none_when_missing() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(main_executable_in("claude", dir.path()).is_none());
    }

    #[test]
    fn resolve_returns_none_in_download_mode() {
        managed_resources::set_managed_resources_mode(ManagedResourcesMode::Download);
        assert!(resolve_bundled_cli("claude").is_none());
    }
}

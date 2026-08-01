use std::path::PathBuf;
use std::sync::Arc;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NativeCliRuntimeKind {
    Native,
    Node,
    Python,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NativeCliToolId {
    Hermes,
    OpenClaw,
}

impl NativeCliToolId {
    pub fn slug(self) -> &'static str {
        match self {
            Self::Hermes => "hermes",
            Self::OpenClaw => "openclaw",
        }
    }

    pub fn version(self) -> &'static str {
        // Pinned versions — mirror pounding/scripts/vendor-versions.env
        // (single source of truth; pounding CI runs check-version-consistency.sh).
        match self {
            Self::Hermes => "0.19.0",
            Self::OpenClaw => "2026.6.33",
        }
    }

    pub fn display_name(self) -> &'static str {
        match self {
            Self::Hermes => "Hermes",
            Self::OpenClaw => "OpenClaw",
        }
    }

    pub fn binary_name(self) -> &'static str {
        match self {
            Self::Hermes => "hermes",
            Self::OpenClaw => "openclaw",
        }
    }

    pub fn from_backend(backend: &str) -> Option<Self> {
        match backend {
            "hermes" => Some(Self::Hermes),
            "openclaw" => Some(Self::OpenClaw),
            _ => None,
        }
    }

    pub fn from_slug(slug: &str) -> Option<Self> {
        match slug {
            "hermes" => Some(Self::Hermes),
            "openclaw" => Some(Self::OpenClaw),
            _ => None,
        }
    }

    pub fn runtime_kind(self) -> NativeCliRuntimeKind {
        match self {
            Self::Hermes => NativeCliRuntimeKind::Python,
            Self::OpenClaw => NativeCliRuntimeKind::Node,
        }
    }

    /// Return the official install command so users get a clear error instead
    /// of a misleading 404 from the dead poundingcore download fallback.
    pub fn install_instruction(self) -> &'static str {
        match self {
            Self::Hermes => "pip install hermes-agent[acp]",
            Self::OpenClaw => "npm install -g openclaw@latest",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedNativeCliTool {
    pub id: NativeCliToolId,
    pub version: String,
    pub root: PathBuf,
    pub binary_path: PathBuf,
}

impl ResolvedNativeCliTool {
    pub fn command(
        &self,
        node_runtime: Option<&crate::node_runtime::ResolvedNodeRuntime>,
    ) -> crate::node_runtime::ResolvedCommand {
        match self.id.runtime_kind() {
            NativeCliRuntimeKind::Node => {
                let node = node_runtime.expect("node runtime required for Node-kind native CLI tool");
                crate::node_runtime::ResolvedCommand {
                    program: node.node_path.clone(),
                    args_prefix: vec![self.binary_path.clone().into_os_string()],
                    env: node.env.clone(),
                }
            }
            NativeCliRuntimeKind::Native | NativeCliRuntimeKind::Python => {
                crate::node_runtime::ResolvedCommand::plain(self.binary_path.clone())
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NativeCliProgressPhase {
    WaitingForLock,
    Downloading,
    Extracting,
    Validating,
    Ready,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NativeCliFailureKind {
    Timeout,
    DownloadFailed,
    HttpStatus,
    ChecksumMismatch,
    ValidationFailed,
    UnsupportedPlatform,
    BundledResourceMissing,
    BundledResourceInvalid,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NativeCliProgress {
    pub phase: NativeCliProgressPhase,
    pub failure_kind: Option<NativeCliFailureKind>,
    pub message: Option<String>,
    pub status_code: Option<u16>,
}

impl NativeCliProgress {
    pub fn waiting_for_lock(message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::WaitingForLock,
            failure_kind: None,
            message: Some(message.into()),
            status_code: None,
        }
    }

    pub fn downloading(message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::Downloading,
            failure_kind: None,
            message: Some(message.into()),
            status_code: None,
        }
    }

    pub fn extracting(message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::Extracting,
            failure_kind: None,
            message: Some(message.into()),
            status_code: None,
        }
    }

    pub fn validating(message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::Validating,
            failure_kind: None,
            message: Some(message.into()),
            status_code: None,
        }
    }

    pub fn ready(message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::Ready,
            failure_kind: None,
            message: Some(message.into()),
            status_code: None,
        }
    }

    pub fn failed(kind: NativeCliFailureKind, message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::Failed,
            failure_kind: Some(kind),
            message: Some(message.into()),
            status_code: None,
        }
    }

    pub fn failed_with_status(kind: NativeCliFailureKind, status_code: u16, message: impl Into<String>) -> Self {
        Self {
            phase: NativeCliProgressPhase::Failed,
            failure_kind: Some(kind),
            message: Some(message.into()),
            status_code: Some(status_code),
        }
    }
}

pub trait NativeCliProgressReporter: Send + Sync {
    fn report(&self, update: NativeCliProgress);
}

impl<F> NativeCliProgressReporter for F
where
    F: Fn(NativeCliProgress) + Send + Sync,
{
    fn report(&self, update: NativeCliProgress) {
        self(update);
    }
}

pub type SharedNativeCliProgressReporter = Arc<dyn NativeCliProgressReporter>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NativeCliToolSupport {
    pub supported: bool,
    pub detail: String,
}

impl NativeCliToolSupport {
    pub fn is_supported(&self) -> bool {
        self.supported
    }
}

#[derive(Debug, Clone, thiserror::Error)]
#[error("{message}")]
pub struct NativeCliToolError {
    message: String,
}

impl NativeCliToolError {
    pub fn invalid(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    pub fn unsupported_platform(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    pub fn io(error: std::io::Error) -> Self {
        Self {
            message: error.to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pinned_versions_match_vendor_versions_env() {
        // Mirror of pounding/scripts/vendor-versions.env — pounding CI runs
        // scripts/check-version-consistency.sh which greps these literals.
        assert_eq!(NativeCliToolId::Hermes.version(), "0.19.0");
        assert_eq!(NativeCliToolId::OpenClaw.version(), "2026.6.33");
    }
}

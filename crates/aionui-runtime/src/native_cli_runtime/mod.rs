mod types;

use std::fs::{self};
use std::path::{Path, PathBuf};

use tracing::{info, warn};

use crate::cache;
use crate::managed_resources;

pub use types::{
    NativeCliFailureKind, NativeCliProgress, NativeCliProgressPhase, NativeCliProgressReporter, NativeCliRuntimeKind,
    NativeCliToolError, NativeCliToolId, NativeCliToolSupport, ResolvedNativeCliTool, SharedNativeCliProgressReporter,
};

static INSTALL_LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();

#[derive(Debug, Clone, Copy)]
struct PlatformSpec {
    manifest_key: &'static str,
}

pub fn probe_native_cli_tool_supported(tool: NativeCliToolId) -> NativeCliToolSupport {
    match platform_spec() {
        Ok(spec) => NativeCliToolSupport {
            supported: true,
            detail: format!(
                "native CLI {} supported under platform {}",
                tool.display_name(),
                spec.manifest_key
            ),
        },
        Err(error) => NativeCliToolSupport {
            supported: false,
            detail: error.to_string(),
        },
    }
}

pub async fn ensure_native_cli_tool(tool: NativeCliToolId) -> Result<ResolvedNativeCliTool, NativeCliToolError> {
    ensure_native_cli_tool_with_reporter(tool, None).await
}

pub async fn ensure_native_cli_tool_with_reporter(
    tool: NativeCliToolId,
    reporter: Option<&dyn NativeCliProgressReporter>,
) -> Result<ResolvedNativeCliTool, NativeCliToolError> {
    let spec = platform_spec().inspect_err(|error| {
        emit_progress(
            reporter,
            NativeCliProgress::failed(NativeCliFailureKind::UnsupportedPlatform, error.to_string()),
        );
    })?;
    let root = tool_root(tool, spec)?;

    // Prefer system-installed CLI on $PATH over managed downloads.
    // Most users already have these tools installed; downloading is a fallback.
    // Skip this in bundled mode: packaged/offline deployments must resolve the
    // tool from the bundled managed resources, not an arbitrary system binary.
    let bin_name = tool.binary_name();
    if !managed_resources::requires_bundled_resources()
        && let Some(system_path) = crate::resolve_command_path(bin_name)
    {
        info!(
            tool = tool.slug(),
            path = %system_path.display(),
            "native CLI found on system PATH; skipping managed download"
        );
        emit_progress(
            reporter,
            NativeCliProgress::ready(format!(
                "native CLI {} found at {}",
                tool.display_name(),
                system_path.display()
            )),
        );
        return Ok(ResolvedNativeCliTool {
            id: tool,
            version: "system".to_owned(),
            root: system_path.parent().map(|p| p.to_path_buf()).unwrap_or_default(),
            binary_path: system_path,
        });
    }

    if let Ok(installed) = validate_tool_root(tool, &root, reporter) {
        return Ok(installed);
    }

    let lock = INSTALL_LOCK.get_or_init(|| tokio::sync::Mutex::new(()));
    let _guard = lock.lock().await;

    if let Ok(installed) = validate_tool_root(tool, &root, reporter) {
        return Ok(installed);
    }

    if let Some(installed) =
        activate_local_tool_source(tool, spec, &root, reporter).map_err(|error| report_failure(error, reporter))?
    {
        return Ok(installed);
    }

    if managed_resources::requires_bundled_resources() {
        return Err(report_failure(
            NativeCliToolError::invalid(format!(
                "bundled native CLI {} artifact missing under the managed resources root",
                tool.display_name()
            )),
            reporter,
        ));
    }

    // No suitable CLI found on PATH or in bundled resources. The download
    // fallback (install_archive_with_retry) pointed at a poundingcore release
    // that has never existed. Instead of a misleading 404, give a clear error
    // telling the user how to install the CLI from the official source.
    Err(report_failure(
        NativeCliToolError::invalid(format!(
            "{} CLI is not installed. Install it via the official source: {}",
            tool.display_name(),
            tool.install_instruction()
        )),
        reporter,
    ))
}

fn validate_tool_root(
    tool: NativeCliToolId,
    root: &Path,
    reporter: Option<&dyn NativeCliProgressReporter>,
) -> Result<ResolvedNativeCliTool, NativeCliToolError> {
    emit_progress(
        reporter,
        NativeCliProgress::validating(format!(
            "validating native CLI {} under {}",
            tool.display_name(),
            root.display()
        )),
    );
    let entrypoint = entrypoint_path(tool, root);
    if !entrypoint.is_file() {
        return Err(NativeCliToolError::invalid(format!(
            "native CLI entrypoint missing: {}",
            entrypoint.display()
        )));
    }
    Ok(ResolvedNativeCliTool {
        id: tool,
        version: tool.version().to_owned(),
        root: root.to_path_buf(),
        binary_path: entrypoint,
    })
}

fn entrypoint_path(tool: NativeCliToolId, root: &Path) -> PathBuf {
    let binary = tool.binary_name();
    match tool.runtime_kind() {
        types::NativeCliRuntimeKind::Node => root.join(binary).join(format!("{binary}.mjs")),
        types::NativeCliRuntimeKind::Native => {
            if cfg!(windows) {
                root.join(format!("{binary}.exe"))
            } else {
                let dot_bin = root.join(".bin").join(binary);
                if dot_bin.exists() { dot_bin } else { root.join(binary) }
            }
        }
        types::NativeCliRuntimeKind::Python => root.join(binary),
    }
}

fn activate_local_tool_source(
    tool: NativeCliToolId,
    spec: PlatformSpec,
    root: &Path,
    reporter: Option<&dyn NativeCliProgressReporter>,
) -> Result<Option<ResolvedNativeCliTool>, NativeCliToolError> {
    let bundled_root = match managed_resources::bundled_root_candidate() {
        Some(r) if r.is_dir() => r,
        _ => return Ok(None),
    };
    let bundled_tool_root = bundled_root
        .join("cli")
        .join(tool.slug())
        .join(tool.version())
        .join(spec.manifest_key);
    if !bundled_tool_root.is_dir() {
        if managed_resources::requires_bundled_resources() {
            return Err(NativeCliToolError::invalid(format!(
                "bundled native CLI {} artifact missing under {}",
                tool.display_name(),
                bundled_tool_root.display()
            )));
        }
        return Ok(None);
    }

    emit_progress(
        reporter,
        NativeCliProgress::extracting(format!(
            "activating native CLI {} from bundled resources",
            tool.display_name()
        )),
    );

    if let Err(error) = managed_resources::materialize_directory(&bundled_tool_root, root) {
        if managed_resources::requires_bundled_resources() {
            return Err(NativeCliToolError::invalid(format!(
                "bundled native CLI {} artifact is invalid under {}: {}",
                tool.display_name(),
                bundled_tool_root.display(),
                error
            )));
        }
        warn!(
            tool = tool.slug(),
            source_root = %bundled_tool_root.display(),
            target_root = %root.display(),
            error = %error,
            "failed to activate bundled native CLI"
        );
        return Ok(None);
    }

    match validate_tool_root(tool, root, reporter) {
        Ok(resolved) => {
            info!(
                tool = tool.slug(),
                version = tool.version(),
                source_root = %bundled_tool_root.display(),
                target_root = %root.display(),
                "native CLI activated from bundled resources"
            );
            Ok(Some(resolved))
        }
        Err(error) => {
            warn!(
                tool = tool.slug(),
                source_root = %bundled_tool_root.display(),
                target_root = %root.display(),
                error = %error,
                "bundled native CLI failed validation"
            );
            let _ = fs::remove_dir_all(root);
            if managed_resources::requires_bundled_resources() {
                Err(NativeCliToolError::invalid(format!(
                    "bundled native CLI {} artifact failed validation under {}: {}",
                    tool.display_name(),
                    bundled_tool_root.display(),
                    error
                )))
            } else {
                Ok(None)
            }
        }
    }
}

fn platform_spec() -> Result<PlatformSpec, NativeCliToolError> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Ok(PlatformSpec {
            manifest_key: "darwin-arm64",
        }),
        ("macos", "x86_64") => Ok(PlatformSpec {
            manifest_key: "darwin-x64",
        }),
        ("linux", "aarch64") => Ok(PlatformSpec {
            manifest_key: "linux-arm64",
        }),
        ("linux", "x86_64") => Ok(PlatformSpec {
            manifest_key: "linux-x64",
        }),
        ("windows", "x86_64") => Ok(PlatformSpec {
            manifest_key: "win32-x64",
        }),
        ("windows", "aarch64") => Ok(PlatformSpec {
            manifest_key: "win32-arm64",
        }),
        (os, arch) => Err(NativeCliToolError::unsupported_platform(format!(
            "native CLI unsupported on {os}/{arch}"
        ))),
    }
}

fn tool_root(tool: NativeCliToolId, spec: PlatformSpec) -> Result<PathBuf, NativeCliToolError> {
    cache::native_cli_tool_root()
        .map(|root| root.join(tool.slug()).join(tool.version()).join(spec.manifest_key))
        .ok_or_else(|| NativeCliToolError::invalid("native CLI runtime cache dir unavailable"))
}

fn emit_progress(reporter: Option<&dyn NativeCliProgressReporter>, update: NativeCliProgress) {
    if let Some(reporter) = reporter {
        reporter.report(update);
    }
}

fn report_failure(error: NativeCliToolError, reporter: Option<&dyn NativeCliProgressReporter>) -> NativeCliToolError {
    let (kind, status_code) = classify_error(&error);
    emit_progress(
        reporter,
        match status_code {
            Some(status) => NativeCliProgress::failed_with_status(kind, status, error.to_string()),
            None => NativeCliProgress::failed(kind, error.to_string()),
        },
    );
    error
}

fn classify_error(error: &NativeCliToolError) -> (NativeCliFailureKind, Option<u16>) {
    let message = error.to_string().to_ascii_lowercase();
    if message.contains("timed out") {
        return (NativeCliFailureKind::Timeout, None);
    }
    if let Some(status) = parse_http_status(&message) {
        return (NativeCliFailureKind::HttpStatus, Some(status));
    }
    if message.contains("unsupported") {
        return (NativeCliFailureKind::UnsupportedPlatform, None);
    }
    if message.contains("bundled native cli") && message.contains("artifact missing") {
        return (NativeCliFailureKind::BundledResourceMissing, None);
    }
    if message.contains("bundled native cli") && message.contains("artifact is invalid") {
        return (NativeCliFailureKind::BundledResourceInvalid, None);
    }
    if message.contains("bundled native cli") && message.contains("failed validation") {
        return (NativeCliFailureKind::BundledResourceInvalid, None);
    }
    if message.contains("checksum mismatch") {
        return (NativeCliFailureKind::ChecksumMismatch, None);
    }
    if message.contains("validate") || message.contains("entrypoint missing") || message.contains("binary missing") {
        return (NativeCliFailureKind::ValidationFailed, None);
    }
    if message.contains("download") || message.contains("extract") || message.contains("connect failed") {
        return (NativeCliFailureKind::DownloadFailed, None);
    }
    (NativeCliFailureKind::Unknown, None)
}

fn parse_http_status(message: &str) -> Option<u16> {
    let marker = "http ";
    let start = message.find(marker)? + marker.len();
    let digits: String = message[start..].chars().take_while(|ch| ch.is_ascii_digit()).collect();
    digits.parse::<u16>().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn platform_spec_macos_arm64_uses_correct_key() {
        if !(cfg!(target_os = "macos") && cfg!(target_arch = "aarch64")) {
            return;
        }
        let spec = platform_spec().expect("macos/arm64 should be supported");
        assert_eq!(spec.manifest_key, "darwin-arm64");
    }

    #[test]
    fn entrypoint_path_resolves_by_runtime_kind() {
        let root = Path::new("/tmp/tool");
        if cfg!(windows) {
            assert_eq!(entrypoint_path(NativeCliToolId::Hermes, root), root.join("hermes"));
            assert_eq!(
                entrypoint_path(NativeCliToolId::OpenCode, root),
                root.join("opencode.exe")
            );
        } else {
            assert_eq!(entrypoint_path(NativeCliToolId::Hermes, root), root.join("hermes"));
            assert_eq!(entrypoint_path(NativeCliToolId::OpenCode, root), root.join("opencode"));
            assert_eq!(
                entrypoint_path(NativeCliToolId::OpenClaw, root),
                root.join("openclaw").join("openclaw.mjs")
            );
        }
    }

    #[test]
    fn validate_tool_root_rejects_missing_entrypoint() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let error =
            validate_tool_root(NativeCliToolId::Hermes, root, None).expect_err("missing entrypoint should fail");
        assert!(error.to_string().contains("entrypoint missing"));
    }

    #[tokio::test]
    async fn bundled_resource_missing_reports_bundled_resource_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let bundled_root = tmp.path().join("bundled");
        if !crate::test_support::run_in_env_child(
            "native_cli_runtime::tests::bundled_resource_missing_reports_bundled_resource_missing",
            |command| {
                command.env("POUNDING_BUNDLED_MANAGED_RESOURCES", &bundled_root);
            },
        ) {
            return;
        }

        crate::cache::init(tmp.path().join("data"));
        managed_resources::set_managed_resources_mode(managed_resources::ManagedResourcesMode::Bundled);

        let result = ensure_native_cli_tool(NativeCliToolId::Hermes).await;
        managed_resources::set_managed_resources_mode(managed_resources::ManagedResourcesMode::Download);

        let error = result.expect_err("missing bundled native CLI should fail");
        assert!(error.to_string().contains("bundled native CLI"));
        assert!(error.to_string().contains("artifact missing"));
    }
}

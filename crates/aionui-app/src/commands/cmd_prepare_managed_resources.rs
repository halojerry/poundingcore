use std::process::ExitCode;

use crate::cli::PrepareManagedResourcesArgs;
use crate::commands::error::{CliBoundaryCode, CliBoundaryError};
use aionui_runtime::ensure_node_runtime;
use aionui_runtime::managed_cli::{managed_cli_contract_for_export, prepare_managed_cli_to_root};
use aionui_runtime::managed_resources::export_node_runtime_to_root;
use aionui_runtime::managed_resources_contract::{
    MANAGED_RESOURCES_CONTRACT_SCHEMA_VERSION, ManagedResourcesContract, validate_contract, write_contract,
};
use aionui_runtime::node_runtime::managed_node_contract_for_export;

const MANAGED_CLI_NAMES: [&str; 1] = ["claude"];

const SUBCOMMAND: &str = "prepare-managed-resources";

pub async fn run_prepare_managed_resources(args: PrepareManagedResourcesArgs) -> Result<ExitCode, CliBoundaryError> {
    let output_root = args.bundle_out;
    std::fs::create_dir_all(&output_root).map_err(|_| prepare_managed_resources_error("output.create"))?;
    // Canonicalize to an absolute path so that all derived paths (staging
    // directories, package roots, and smoke test targets) are absolute.
    // Without this, a relative --bundle-out (e.g. `./managed-resources`)
    // causes Node subprocesses to resolve relative paths against a different
    // CWD and produce doubled staging paths.
    let output_root =
        std::fs::canonicalize(&output_root).map_err(|_| prepare_managed_resources_error("output.canonicalize"))?;

    let node_runtime = ensure_node_runtime()
        .await
        .map_err(|error| prepare_managed_resources_error_with_detail("node.prepare", error))?;
    let node_dir_name = node_runtime
        .root
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| prepare_managed_resources_error("node.layout"))?;
    let exported_node = export_node_runtime_to_root(&output_root, &node_runtime.root, node_dir_name)
        .map_err(|error| prepare_managed_resources_error_with_detail("node.export", error))?;

    println!("Prepared managed resources under {}", output_root.display());
    println!("  node   -> {}", exported_node.display());

    let mut prepared_clis = Vec::new();
    for name in MANAGED_CLI_NAMES {
        let prepared = prepare_managed_cli_to_root(name, &output_root)
            .await
            .map_err(|error| prepare_managed_resources_error_with_detail("cli.prepare", error))?;
        // Write a per-CLI manifest.json so the desktop app's
        // resolveBundledCliDir() / materializeFromBundled() can discover the
        // native binary for offline PATH registration. Without this the TS
        // bundled fallback was dead code (PRD offline-oob-cli-install-fixes).
        write_cli_manifest(&prepared.root, &prepared.executable)
            .map_err(|error| prepare_managed_resources_error_with_detail("cli.manifest", error))?;
        println!("  {:<6} -> {}", name, prepared.root.display());
        prepared_clis.push(prepared);
    }

    let node = managed_node_contract_for_export(&output_root, &exported_node)
        .map_err(|error| prepare_managed_resources_error_with_detail("contract.write", error))?;
    let mut clis = Vec::new();
    for prepared in &prepared_clis {
        clis.push(
            managed_cli_contract_for_export(&output_root, prepared)
                .map_err(|error| prepare_managed_resources_error_with_detail("contract.write", error))?,
        );
    }
    let runtime_key = clis
        .first()
        .map(|cli| cli.platform_directory.clone())
        .ok_or_else(|| prepare_managed_resources_error("contract.write"))?;
    let contract = ManagedResourcesContract {
        schema_version: MANAGED_RESOURCES_CONTRACT_SCHEMA_VERSION,
        runtime_key,
        node,
        clis,
    };
    let manifest_path = write_contract(&output_root, &contract)
        .map_err(|error| prepare_managed_resources_error_with_detail("contract.write", error))?;
    validate_contract(&output_root, &contract)
        .map_err(|error| prepare_managed_resources_error_with_detail("contract.validate", error))?;
    println!("  manifest -> {}", manifest_path.display());

    Ok(ExitCode::SUCCESS)
}

#[allow(dead_code)]
fn detect_platform_key() -> &'static str {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "darwin-arm64",
        ("macos", "x86_64") => "darwin-x64",
        ("linux", "aarch64") => "linux-arm64",
        ("linux", "x86_64") => "linux-x64",
        ("windows", "x86_64") => "win32-x64",
        ("windows", "aarch64") => "win32-arm64",
        _ => "unknown",
    }
}

#[allow(dead_code)]
fn copy_directory(src: &std::path::Path, dest: &std::path::Path) -> Result<(), String> {
    if !src.is_dir() {
        return Err(format!("source directory missing: {}", src.display()));
    }
    std::fs::create_dir_all(dest).map_err(|e| format!("create dest dir {dest:?}: {e}"))?;
    for entry in std::fs::read_dir(src).map_err(|e| format!("read dir {src:?}: {e}"))? {
        let entry = entry.map_err(|e| format!("read entry: {e}"))?;
        let src_path = entry.path();
        let dest_path = dest.join(entry.file_name());
        if src_path.is_dir() {
            copy_directory(&src_path, &dest_path)?;
        } else {
            std::fs::copy(&src_path, &dest_path).map_err(|e| format!("copy file {src_path:?}: {e}"))?;
        }
    }
    Ok(())
}

/// Manifest written alongside each bundled native CLI tool so the
/// frontend's `materializeFromBundled()` can discover the entrypoint
/// (`resolveBundledCliDir` requires a manifest.json in the CLI directory).
///
/// `kind: "native"` signals verify-bundle-integrity.sh that version/platform
/// fields are optional. `entrypoint` carries the platform executable name
/// (e.g. `claude.exe` on Windows — matches prepare.rs `exe_suffix()`).
#[derive(Debug, serde::Serialize)]
struct CliManifest {
    entrypoint: String,
    kind: &'static str,
}

fn write_cli_manifest(dest_dir: &std::path::Path, executable: &str) -> Result<(), String> {
    let manifest = CliManifest {
        entrypoint: executable.to_owned(),
        kind: "native",
    };
    let manifest_path = dest_dir.join("manifest.json");
    std::fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&manifest).map_err(|e| format!("serialize manifest.json: {e}"))?,
    )
    .map_err(|e| format!("write manifest.json: {e}"))?;

    Ok(())
}

fn prepare_managed_resources_error(stage: &'static str) -> CliBoundaryError {
    CliBoundaryError::new(
        CliBoundaryCode::CliPrepareManagedResourcesFailed,
        SUBCOMMAND,
        "failed to prepare managed resources",
    )
    .with_field("stage", stage)
}

fn prepare_managed_resources_error_with_detail(stage: &'static str, error: impl std::fmt::Display) -> CliBoundaryError {
    eprintln!("prepare-managed-resources stage={stage} detail: {error}");
    prepare_managed_resources_error(stage)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepare_error_uses_stable_code_and_stage_without_raw_path() {
        let err = prepare_managed_resources_error("node.export");

        assert_eq!(err.code(), CliBoundaryCode::CliPrepareManagedResourcesFailed);
        assert!(err.stderr_line().starts_with(
            "CLI_PREPARE_MANAGED_RESOURCES_FAILED subcommand=prepare-managed-resources stage=node.export"
        ));
        assert!(!err.stderr_line().contains("/Users/secret/bundle"));
    }

    #[test]
    fn prepare_error_accepts_contract_write_and_validate_stages() {
        for stage in ["contract.write", "contract.validate"] {
            let err = prepare_managed_resources_error(stage);
            assert_eq!(err.code(), CliBoundaryCode::CliPrepareManagedResourcesFailed);
            assert!(err.stderr_line().contains(stage));
        }
    }
}

#![allow(clippy::disallowed_types)]

//! Doctor HTTP routes (`/api/doctor/*`).
//!
//! The desktop app's startup self-check (`DoctorService.ts`) calls
//! these to diagnose agent CLI availability and trigger a backend-side
//! re-probe when a bundled install just landed. Without them the
//! frontend fell back to an empty report and the cli-prep
//! diagnose/repair loop was dead code.

use std::collections::HashMap;

use axum::Router;
use axum::extract::rejection::JsonRejection;
use axum::extract::{Json, State};
use axum::routing::{get, post};

use aionui_api_types::{AgentMetadata, ApiResponse};
use aionui_common::ApiError;
use aionui_runtime::doctor_snapshot;

use crate::registry::UnavailableReason;
use crate::routes::state::AgentRouterState;

/// Wire shape of `GET /api/doctor/diagnose`.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentDiagnosticReport {
    pub agents: Vec<AgentDiagnosticRow>,
    pub runtimes: HashMap<String, ToolAvailability>,
    pub acp_bridges: HashMap<String, ToolAvailability>,
    pub summary: DiagnosticSummary,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentDiagnosticRow {
    pub name: String,
    pub backend: Option<String>,
    pub available: bool,
    pub reason: Option<String>,
    pub bundled_source: bool,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolAvailability {
    pub available: bool,
    pub path: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DiagnosticSummary {
    pub healthy: bool,
    pub issues: Vec<String>,
}

/// Wire shape of `POST /api/doctor/repair`.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct DoctorRepairRequest {
    pub target: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DoctorRepairResult {
    pub success: bool,
    pub source: Option<String>,
    pub error: Option<String>,
}

pub fn doctor_routes(state: AgentRouterState) -> Router {
    Router::new()
        .route("/api/doctor/diagnose", get(diagnose))
        .route("/api/doctor/repair", post(repair))
        .with_state(state)
}

async fn diagnose(State(state): State<AgentRouterState>) -> Result<Json<ApiResponse<AgentDiagnosticReport>>, ApiError> {
    let snapshot = state.agent_registry.diagnostic_snapshot().await;
    let report = build_report(&snapshot);
    Ok(Json(ApiResponse::ok(report)))
}

async fn repair(
    State(state): State<AgentRouterState>,
    body: Result<Json<DoctorRepairRequest>, JsonRejection>,
) -> Result<Json<ApiResponse<DoctorRepairResult>>, ApiError> {
    let Json(req) = body.map_err(ApiError::from)?;
    let target = req.target.trim().to_lowercase();
    if target.is_empty() {
        return Ok(Json(ApiResponse::ok(DoctorRepairResult {
            success: false,
            source: Some("backend".to_owned()),
            error: Some("empty target".to_owned()),
        })));
    }

    // Find the agent whose name / backend / spawn binary matches the
    // requested target, then re-probe just that row. Use the unfiltered
    // catalog: `list_all()` drops rows that haven't got an availability
    // snapshot yet (status `Unchecked` on a fresh install), which would
    // make the repair loop unable to find the very agent it was asked
    // to fix.
    let all = state.agent_registry.list_all_including_hidden().await;
    let matched = all.iter().find(|meta| matches_target(meta, &target)).cloned();

    let Some(meta) = matched else {
        return Ok(Json(ApiResponse::ok(DoctorRepairResult {
            success: false,
            source: Some("backend".to_owned()),
            error: Some(format!("no agent matches target `{target}`")),
        })));
    };

    let refreshed = state.agent_registry.reload_one(&meta.id).await;
    match refreshed {
        Ok(Some(updated)) => Ok(Json(ApiResponse::ok(DoctorRepairResult {
            success: updated.available,
            source: Some("backend".to_owned()),
            error: if updated.available {
                None
            } else {
                Some(format!("agent `{}` still unavailable after re-probe", updated.name))
            },
        }))),
        Ok(None) => Ok(Json(ApiResponse::ok(DoctorRepairResult {
            success: false,
            source: Some("backend".to_owned()),
            error: Some(format!("agent `{}` no longer in catalog", meta.name)),
        }))),
        Err(error) => Ok(Json(ApiResponse::ok(DoctorRepairResult {
            success: false,
            source: Some("backend".to_owned()),
            error: Some(format!("re-probe failed: {error}")),
        }))),
    }
}

fn matches_target(meta: &AgentMetadata, target: &str) -> bool {
    let target = target.to_lowercase();
    if meta.name.to_lowercase().contains(&target) {
        return true;
    }
    if meta
        .backend
        .as_deref()
        .map(|b| b.to_lowercase() == target)
        .unwrap_or(false)
    {
        return true;
    }
    meta.agent_source_info
        .binary_name
        .as_deref()
        .or(meta.agent_source_info.bridge_binary.as_deref())
        .map(|b| b.to_lowercase() == target)
        .unwrap_or(false)
}

fn build_report(snapshot: &[(AgentMetadata, Option<UnavailableReason>)]) -> AgentDiagnosticReport {
    let mut agents = Vec::with_capacity(snapshot.len());
    let mut issues = Vec::new();

    for (meta, reason) in snapshot {
        let reason_text = reason.as_ref().map(ToString::to_string);
        let bundled_source =
            meta.agent_source_info.binary_name.is_some() || meta.agent_source_info.bridge_binary.is_some();
        if !meta.available {
            issues.push(format!(
                "{}: {}",
                meta.name,
                reason_text.clone().unwrap_or_else(|| "unavailable".to_owned())
            ));
        }
        agents.push(AgentDiagnosticRow {
            name: meta.name.clone(),
            backend: meta.backend.clone(),
            available: meta.available,
            reason: reason_text,
            bundled_source,
        });
    }

    // Node/npm/npx runtime resolution from the shared runtime crate.
    let mut runtimes = HashMap::new();
    for row in doctor_snapshot() {
        runtimes.insert(
            row.tool,
            ToolAvailability {
                available: row.source != "unavailable",
                path: Some(row.detail),
            },
        );
    }

    // ACP bridges = managed CLI agents with a declared primary binary.
    let mut acp_bridges = HashMap::new();
    for (meta, _) in snapshot {
        if let Some(binary) = meta.agent_source_info.binary_name.as_deref() {
            acp_bridges.insert(
                binary.to_owned(),
                ToolAvailability {
                    available: meta.available,
                    path: meta.resolved_command.as_ref().map(|p| p.display().to_string()),
                },
            );
        }
    }

    AgentDiagnosticReport {
        agents,
        runtimes,
        acp_bridges,
        summary: DiagnosticSummary {
            healthy: issues.is_empty(),
            issues,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use aionui_api_types::{AgentSource, AgentSourceInfo};
    use aionui_common::AgentType;

    fn sample_meta(name: &str, backend: Option<&str>, available: bool, binary: Option<&str>) -> AgentMetadata {
        AgentMetadata {
            id: format!("agent-{name}"),
            icon: None,
            name: name.to_owned(),
            name_i18n: None,
            description: None,
            description_i18n: None,
            backend: backend.map(ToOwned::to_owned),
            agent_type: AgentType::Acp,
            agent_source: AgentSource::Builtin,
            agent_source_info: AgentSourceInfo {
                binary_name: binary.map(ToOwned::to_owned),
                bridge_binary: None,
                hub_package_id: None,
                version: None,
            },
            enabled: true,
            available,
            command: None,
            resolved_command: None,
            args: Vec::new(),
            env: Vec::new(),
            native_skills_dirs: None,
            behavior_policy: Default::default(),
            yolo_id: None,
            sort_order: 0,
            team_capable: false,
            handshake: Default::default(),
            last_check_status: None,
            last_check_kind: None,
            last_check_error_code: None,
            last_check_error_message: None,
            last_check_error_details: None,
            last_check_guidance: None,
            last_check_latency_ms: None,
            last_check_at: None,
            last_success_at: None,
            last_failure_at: None,
            has_command_override: false,
            env_override_key_count: 0,
        }
    }

    #[test]
    fn report_flags_unavailable_agents_with_reason() {
        let snapshot = vec![
            (sample_meta("Claude Code", Some("claude"), true, Some("claude")), None),
            (
                sample_meta("Hermes", Some("hermes"), false, Some("hermes")),
                Some(UnavailableReason::PrimaryMissing {
                    binary: "hermes".to_owned(),
                }),
            ),
        ];
        let report = build_report(&snapshot);
        assert_eq!(report.agents.len(), 2);
        assert!(!report.summary.healthy);
        assert!(report.summary.issues[0].contains("Hermes"));
        assert!(report.agents[1].bundled_source);
        assert!(report.acp_bridges.contains_key("hermes"));
        assert!(!report.acp_bridges["hermes"].available);
    }

    #[test]
    fn report_healthy_when_all_agents_available() {
        let snapshot = vec![(sample_meta("Claude Code", Some("claude"), true, Some("claude")), None)];
        let report = build_report(&snapshot);
        assert!(report.summary.healthy);
        assert!(report.summary.issues.is_empty());
    }

    #[test]
    fn target_matches_name_backend_and_binary() {
        let meta = sample_meta("Claude Code", Some("claude"), true, Some("claude"));
        assert!(matches_target(&meta, "claude"));
        assert!(matches_target(&meta, "CLAUDE"));
        assert!(matches_target(&meta, "claude code"));
        assert!(!matches_target(&meta, "hermes"));
    }
}

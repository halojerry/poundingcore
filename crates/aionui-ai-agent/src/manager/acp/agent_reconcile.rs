use crate::manager::acp::AcpAgentManager;

use crate::manager::acp::error_mapping::is_acp_session_not_found;
use crate::manager::acp::mode_normalize::normalize_requested_mode_for_available_values;
use crate::protocol::error::AcpError;
use crate::shared_kernel::{ConfigKey, ConfigValue, ModeId, ModelId};
use agent_client_protocol::schema::v1::{SessionId, SetSessionConfigOptionRequest, SetSessionModeRequest};
use std::collections::VecDeque;
use tracing::{debug, error, info, warn};

/// Actions the session driver must execute to align CLI state with user intent.
///
/// Produced by `AcpSession::plan_reconcile` — a pure function that compares
/// desired vs observed and returns a list of idempotent, order-independent ops.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReconcileAction {
    SetMode { mode: ModeId },
    SetModel { model: ModelId },
    SetConfigOption { key: ConfigKey, value: ConfigValue },
}

impl AcpAgentManager {
    /// Execute reconcile actions produced by `AcpSession::plan_reconcile`.
    ///
    /// Compares the aggregate's desired state against what the CLI has
    /// reported as current, then issues the minimal set of SDK calls
    /// (set_mode, set_model, set_config_option) to bring the CLI into
    /// alignment.
    ///
    /// Failure handling:
    /// - `SessionNotFound`: returned as structured `AcpError::SessionNotFound` so callers
    ///   (e.g. `open_session_resume`) can drop the stale sid and rebuild
    ///   the session. ELECTRON-1HQ regressed because we silently swallowed
    ///   this case during warmup, leaving downstream `session/prompt` to
    ///   surface the same error to the user every turn.
    /// - Any other error: logged and skipped (best-effort), so a failed
    ///   `set_config_option` doesn't block a successful `set_mode`.
    pub(super) async fn reconcile_session(&self, session_id: &str) -> Result<(), AcpError> {
        use crate::manager::acp::ReconcileAction;

        let (startup_config_seed_results, invalid_mode, invalid_model, actions) = {
            let mut session = self.session.write().await;
            let startup_config_seed_results =
                session.resolve_pending_startup_config_seeds_with_mode_normalizer(|requested, available_values| {
                    normalize_requested_mode_for_available_values(
                        &self.params.metadata,
                        requested,
                        available_values.iter().copied(),
                    )
                });
            let invalid_mode = session.clear_invalid_desired_mode();
            let invalid_model = session.clear_invalid_desired_model();
            let actions = session.plan_reconcile();
            (startup_config_seed_results, invalid_mode, invalid_model, actions)
        };
        let mut actions: VecDeque<_> = actions.into();
        if !startup_config_seed_results.is_empty() {
            debug!(
                conversation_id = %self.params.conversation_id,
                results = ?startup_config_seed_results,
                "reconcile_session: resolved pending startup config seeds"
            );
        }
        if let Some(mode) = invalid_mode {
            warn!(
                conversation_id = %self.params.conversation_id,
                mode_id = %mode,
                "reconcile_session: dropped unavailable desired mode"
            );
        }
        if let Some(model) = invalid_model {
            warn!(
                conversation_id = %self.params.conversation_id,
                model_id = %model,
                "reconcile_session: dropped unavailable desired model"
            );
        }
        while let Some(action) = actions.pop_front() {
            match action {
                ReconcileAction::SetMode { mode } => {
                    let normalized = {
                        let session = self.session.read().await;
                        let snapshot = session.config_snapshot();
                        normalize_requested_mode_for_available_values(
                            &self.params.metadata,
                            mode.as_str(),
                            snapshot.selectable_values("mode"),
                        )
                    };
                    if normalized.is_empty() {
                        continue;
                    }
                    if let Err(e) = self
                        .protocol
                        .set_mode(SetSessionModeRequest::new(
                            SessionId::new(session_id),
                            normalized.clone(),
                        ))
                        .await
                    {
                        if is_acp_session_not_found(&e) {
                            warn!(
                                conversation_id = %self.params.conversation_id,
                                mode_id = %normalized,
                                error = %e,
                                "reconcile_session: set_mode hit SessionNotFound; aborting reconcile"
                            );
                            return Err(e);
                        }
                        error!(
                            conversation_id = %self.params.conversation_id,
                            mode_id = %normalized,
                            error = %e,
                            "reconcile_session: set_mode failed"
                        );
                        continue;
                    }
                    // SDK does not push a notification after a successful
                    // set_mode — sync observed/advertised ourselves so the
                    // next plan_reconcile is a no-op.
                    let mut session = self.session.write().await;
                    session.apply_observed_mode(ModeId::new(normalized));
                    self.commit_session_changes(&mut session).await;
                }

                ReconcileAction::SetModel { model } => {
                    if let Err(e) = self.protocol.set_model(session_id, model.as_str()).await {
                        if is_acp_session_not_found(&e) {
                            warn!(
                                conversation_id = %self.params.conversation_id,
                                model_id = %model,
                                error = %e,
                                "reconcile_session: set_model hit SessionNotFound; aborting reconcile"
                            );
                            return Err(e);
                        }
                        error!(
                            conversation_id = %self.params.conversation_id,
                            model_id = %model,
                            error = %e,
                            "reconcile_session: set_model failed"
                        );
                        continue;
                    }
                    // SDK does not push a CurrentModelUpdate notification —
                    // sync observed/advertised ourselves.
                    let mut session = self.session.write().await;
                    let model_for_notice = model.clone();
                    session.apply_observed_model(model);
                    if self.params.metadata.behavior_policy.self_identity_sticky {
                        session.set_pending_model_notice(model_for_notice);
                    }
                    self.commit_session_changes(&mut session).await;
                }

                ReconcileAction::SetConfigOption { key, value } => {
                    let resolved_value = if key.as_str() == "mode" {
                        let session = self.session.read().await;
                        let snapshot = session.config_snapshot();
                        normalize_requested_mode_for_available_values(
                            &self.params.metadata,
                            value.as_str(),
                            snapshot.selectable_values("mode"),
                        )
                    } else {
                        value.as_str().trim().to_owned()
                    };
                    if key.as_str() == "mode" && resolved_value != value.as_str() {
                        let mut session = self.session.write().await;
                        session.set_desired_config(key.clone(), ConfigValue::new(resolved_value.clone()));
                    }
                    info!(
                        conversation_id = %self.params.conversation_id,
                        agent_backend = ?self.params.metadata.backend,
                        config_id = %key,
                        desired = %resolved_value,
                        "acp_reconcile_config_option_requested"
                    );
                    let executed_action = ReconcileAction::SetConfigOption {
                        key: key.clone(),
                        value: ConfigValue::new(resolved_value.clone()),
                    };
                    match self
                        .protocol
                        .set_config_option(SetSessionConfigOptionRequest::new(
                            SessionId::new(session_id),
                            key.as_str().to_owned(),
                            resolved_value.as_str(),
                        ))
                        .await
                    {
                        Ok(response) => {
                            info!(
                                conversation_id = %self.params.conversation_id,
                                agent_backend = ?self.params.metadata.backend,
                                config_id = %key,
                                desired = %resolved_value,
                                "acp_reconcile_config_option_ack"
                            );
                            let (startup_config_seed_results, invalid_mode, invalid_model, followup_actions) = {
                                let mut session = self.session.write().await;
                                session.apply_advertised_config_options(response.config_options);
                                let startup_config_seed_results = session
                                    .resolve_pending_startup_config_seeds_with_mode_normalizer(
                                        |requested, available_values| {
                                            normalize_requested_mode_for_available_values(
                                                &self.params.metadata,
                                                requested,
                                                available_values.iter().copied(),
                                            )
                                        },
                                    );
                                let invalid_mode = session.clear_invalid_desired_mode();
                                let invalid_model = session.clear_invalid_desired_model();
                                let followup_actions = session.plan_reconcile();
                                self.commit_session_changes(&mut session).await;
                                (
                                    startup_config_seed_results,
                                    invalid_mode,
                                    invalid_model,
                                    followup_actions,
                                )
                            };
                            if !startup_config_seed_results.is_empty() {
                                debug!(
                                    conversation_id = %self.params.conversation_id,
                                    results = ?startup_config_seed_results,
                                    "reconcile_session: resolved pending startup config seeds (followup)"
                                );
                            }
                            if let Some(mode) = invalid_mode {
                                warn!(
                                    conversation_id = %self.params.conversation_id,
                                    mode_id = %mode,
                                    "reconcile_session: dropped unavailable desired mode (followup)"
                                );
                            }
                            if let Some(model) = invalid_model {
                                warn!(
                                    conversation_id = %self.params.conversation_id,
                                    model_id = %model,
                                    "reconcile_session: dropped unavailable desired model (followup)"
                                );
                            }
                            let mut followup_actions = followup_actions;
                            followup_actions.retain(|candidate| candidate != &executed_action);
                            actions.extend(followup_actions);
                        }
                        Err(err) => {
                            if is_acp_session_not_found(&err) {
                                warn!(
                                    conversation_id = %self.params.conversation_id,
                                    config_id = %key,
                                    desired = %resolved_value,
                                    error = %err,
                                    "reconcile_session: set_config_option hit SessionNotFound; aborting reconcile"
                                );
                                return Err(err);
                            }
                            info!(
                                conversation_id = %self.params.conversation_id,
                                config_id = %key,
                                desired = %resolved_value,
                                error = %err,
                                "reconcile_session: set_config_option failed; skipping"
                            );
                            continue;
                        }
                    }
                    // Sync observed ourselves so the next plan_reconcile
                    // does not replay this action. CLI does not push a
                    // config-update notification after set_config_option.
                    let mut session = self.session.write().await;
                    session.apply_observed_config(key, value);
                    self.commit_session_changes(&mut session).await;
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reconcile_action_equality() {
        let a = ReconcileAction::SetMode {
            mode: ModeId::new("plan"),
        };
        let b = ReconcileAction::SetMode {
            mode: ModeId::new("plan"),
        };
        assert_eq!(a, b);
    }
}

//! Agent-session operations on ConversationService.
//!
//! These forward to the active AgentInstance (via `self.task(id)`) for
//! mode/model/usage/slash-commands/config-options/side-question queries,
//! plus workspace browsing that needs the conversations.extra.workspace field.
//!
//! Kept in a separate file from service.rs to avoid pushing that file
//! over 2000 lines.

use std::path::Component;

use aionui_ai_agent::{AcpError, AgentError};
use aionui_api_types::{
    AgentModeResponse, ConfigOptionConfirmation, GetConfigOptionsResponse, GetModelInfoResponse,
    SetConfigOptionRequest, SetConfigOptionResponse, SetModeRequest, SetModelRequest, SideQuestionRequest,
    SideQuestionResponse, SlashCommandItem, WorkspaceBrowseQuery, WorkspaceEntry,
};
use aionui_common::{AgentKillReason, ErrorChain};
use tracing::warn;

use crate::ConversationError;
use crate::service::{AssistantRuntimePreferenceUpdate, ConversationService};

const MAX_DIR_DEPTH: usize = 10;

impl ConversationService {
    // ── Mode ────────────────────────────────────────────────────────

    pub async fn get_mode(&self, user_id: &str, conversation_id: &str) -> Result<AgentModeResponse, ConversationError> {
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        self.task(conversation_id)?
            .get_mode()
            .await
            .map_err(ConversationError::from)
    }

    pub async fn set_mode(
        &self,
        user_id: &str,
        conversation_id: &str,
        req: SetModeRequest,
    ) -> Result<AgentModeResponse, ConversationError> {
        if req.mode.trim().is_empty() {
            return Err(ConversationError::BadRequest {
                reason: "mode must not be empty".into(),
            });
        }
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        let task = self.task(conversation_id)?;
        // Route the mode switch through the unified config-options chokepoint:
        // the ACP manager's `set_config_option_confirmed` (mode is a select
        // option in the config-option catalog) and the direct-CLI session
        // backend both implement it.
        task.set_config_option("mode", &req.mode)
            .await
            .map_err(ConversationError::from)?;
        // Persist the mode change for recovery on restart. These are
        // independent writes; a partial failure is non-fatal — the
        // agent is already in the new mode and will re-sync on next
        // reconcile.
        if let Err(e) = self
            .persist_runtime_assistant_snapshot(
                user_id,
                conversation_id,
                crate::service::AssistantRuntimePreferenceUpdate {
                    permission: Some(&req.mode),
                    ..Default::default()
                },
            )
            .await
        {
            tracing::warn!(conversation_id, mode = %req.mode, error = %e, "Failed to persist mode to snapshot");
        }
        if let Err(e) = self
            .persist_runtime_assistant_preferences(
                user_id,
                conversation_id,
                crate::service::AssistantRuntimePreferenceUpdate {
                    permission: Some(&req.mode),
                    ..Default::default()
                },
            )
            .await
        {
            tracing::warn!(conversation_id, mode = %req.mode, error = %e, "Failed to persist mode to preferences");
        }
        task.get_mode().await.map_err(ConversationError::from)
    }

    // ── Model ───────────────────────────────────────────────────────

    pub async fn get_model(
        &self,
        user_id: &str,
        conversation_id: &str,
    ) -> Result<GetModelInfoResponse, ConversationError> {
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        self.task(conversation_id)?
            .get_model()
            .await
            .map_err(ConversationError::from)
    }

    pub async fn set_model(
        &self,
        user_id: &str,
        conversation_id: &str,
        req: SetModelRequest,
    ) -> Result<GetModelInfoResponse, ConversationError> {
        if req.model_id.trim().is_empty() {
            return Err(ConversationError::BadRequest {
                reason: "model_id must not be empty".into(),
            });
        }
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        let task = self.task(conversation_id)?;
        // Same unified chokepoint as `set_mode`: model is a select option in
        // the config-option catalog.
        task.set_config_option("model", &req.model_id)
            .await
            .map_err(ConversationError::from)?;
        // Persist the model change for recovery on restart. These are
        // independent writes; a partial failure is non-fatal — the
        // agent is already confirmed in the new model.
        if let Err(e) = self
            .persist_runtime_assistant_snapshot(
                user_id,
                conversation_id,
                crate::service::AssistantRuntimePreferenceUpdate {
                    model: Some(&req.model_id),
                    ..Default::default()
                },
            )
            .await
        {
            tracing::warn!(conversation_id, model_id = %req.model_id, error = %e, "Failed to persist model to snapshot");
        }
        if let Err(e) = self
            .persist_runtime_assistant_preferences(
                user_id,
                conversation_id,
                crate::service::AssistantRuntimePreferenceUpdate {
                    model: Some(&req.model_id),
                    ..Default::default()
                },
            )
            .await
        {
            tracing::warn!(conversation_id, model_id = %req.model_id, error = %e, "Failed to persist model to preferences");
        }
        task.get_model().await.map_err(ConversationError::from)
    }

    // ── Config Options ──────────────────────────────────────────────

    pub async fn get_config_options(
        &self,
        user_id: &str,
        conversation_id: &str,
    ) -> Result<GetConfigOptionsResponse, ConversationError> {
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        match self.task(conversation_id) {
            Ok(task) => task.get_config_options().await.map_err(ConversationError::from),
            Err(ConversationError::ActiveAgentNotFound { .. }) => {
                Ok(GetConfigOptionsResponse { config_options: vec![] })
            }
            Err(e) => Err(e),
        }
    }

    pub async fn set_config_option(
        &self,
        user_id: &str,
        conversation_id: &str,
        option_id: &str,
        req: SetConfigOptionRequest,
    ) -> Result<SetConfigOptionResponse, ConversationError> {
        if option_id.trim().is_empty() {
            return Err(ConversationError::BadRequest {
                reason: "option_id must not be empty".into(),
            });
        }
        if req.value.trim().is_empty() {
            return Err(ConversationError::BadRequest {
                reason: "value must not be empty".into(),
            });
        }
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        let agent = self.task(conversation_id)?;
        let response = match agent.set_config_option(option_id, &req.value).await {
            Ok(response) => response,
            Err(err @ AgentError::Acp(AcpError::NotConnected)) => {
                warn!(
                    conversation_id,
                    option_id,
                    value = %req.value,
                    error = %ErrorChain(&err),
                    "Set config option skipped because active agent task is unavailable"
                );
                return Err(ConversationError::from(err));
            }
            Err(err) => return Err(ConversationError::from(err)),
        };

        // Mirror runtime model/mode/thought-level switches into the persisted assistant
        // snapshot + preference so the next conversation seeded from this
        // assistant in `auto` mode reflects the latest pick. We only act on
        // observed confirmations — `command_ack` means the agent merely
        // accepted the request, not that the value is in effect. Persistence
        // failures are logged but do not roll back the
        // user-facing config switch.
        if response.confirmation == ConfigOptionConfirmation::Observed {
            let category = response
                .config_options
                .as_ref()
                .and_then(|options| options.iter().find(|option| option.id == option_id))
                .and_then(|option| option.category.as_deref())
                .unwrap_or(option_id);
            let updates = match category {
                "model" => Some(AssistantRuntimePreferenceUpdate {
                    model: Some(req.value.as_str()),
                    permission: None,
                    thought_level: None,
                }),
                "mode" => Some(AssistantRuntimePreferenceUpdate {
                    model: None,
                    permission: Some(req.value.as_str()),
                    thought_level: None,
                }),
                "thought_level" | "reasoning_effort" => Some(AssistantRuntimePreferenceUpdate {
                    model: None,
                    permission: None,
                    thought_level: Some(req.value.as_str()),
                }),
                _ => None,
            };
            if let Some(updates) = updates {
                if let Err(err) = self
                    .persist_runtime_assistant_snapshot(user_id, conversation_id, updates)
                    .await
                {
                    warn!(
                        conversation_id,
                        option_id,
                        error = %ErrorChain(&err),
                        "Failed to persist runtime assistant snapshot after set_config_option",
                    );
                }
                if let Err(err) = self
                    .persist_runtime_assistant_preferences(user_id, conversation_id, updates)
                    .await
                {
                    warn!(
                        conversation_id,
                        option_id,
                        error = %ErrorChain(&err),
                        "Failed to persist runtime assistant preferences after set_config_option",
                    );
                }
            }
        }

        Ok(response)
    }

    // ── Usage / Slash commands ──────────────────────────────────────

    pub async fn get_usage(
        &self,
        user_id: &str,
        conversation_id: &str,
    ) -> Result<Option<serde_json::Value>, ConversationError> {
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        // A reaped task must NOT mean "no usage". The indicator's whole point is
        // to survive switching away and back, and the snapshot it needs is
        // already durable in `acp_session.session_config.runtime.context_usage`
        // — `SessionAgentTask::get_usage` reads it from there too. Requiring a
        // live task here made the figure vanish exactly when the user returned
        // to an idle conversation.
        if let Ok(task) = self.task(conversation_id) {
            return task.get_usage().await.map_err(ConversationError::from);
        }
        let state = self
            .acp_session_repo()
            .load_runtime_state_for_user(user_id, conversation_id)
            .await
            .map_err(|e| ConversationError::internal(format!("Failed to load usage state: {e}")))?;
        Ok(state
            .and_then(|s| s.context_usage_json)
            .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok()))
    }

    pub async fn get_slash_commands(
        &self,
        user_id: &str,
        conversation_id: &str,
    ) -> Result<Vec<SlashCommandItem>, ConversationError> {
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        match self.task(conversation_id) {
            Ok(task) => task.get_slash_commands().await.map_err(ConversationError::from),
            Err(ConversationError::ActiveAgentNotFound { .. }) => Ok(Vec::new()),
            Err(e) => Err(e),
        }
    }

    // ── Side question ───────────────────────────────────────────────

    pub async fn handle_side_question(
        &self,
        user_id: &str,
        conversation_id: &str,
        req: SideQuestionRequest,
    ) -> Result<SideQuestionResponse, ConversationError> {
        self.ensure_owned_conversation(user_id, conversation_id).await?;
        // `AgentInstance::handle_side_question` already validates that the
        // question is non-empty; no need to duplicate the check here.
        self.task(conversation_id)?
            .handle_side_question(req)
            .await
            .map_err(ConversationError::from)
    }

    async fn ensure_owned_conversation(&self, user_id: &str, conversation_id: &str) -> Result<(), ConversationError> {
        let exists = self
            .conversation_repo()
            .get(user_id, conversation_id)
            .await
            .map_err(|e| ConversationError::internal(format!("Failed to load conversation: {e}")))?
            .is_some();
        if exists {
            Ok(())
        } else {
            Err(ConversationError::NotFound {
                id: conversation_id.to_owned(),
            })
        }
    }

    // ── Workspace browsing ──────────────────────────────────────────

    /// Enumerate entries under `query.path` inside the conversation's
    /// workspace root. Enforces workspace isolation (no traversal outside
    /// the root, with an allowance for symlinked sub-directories) and a
    /// depth cap of [`MAX_DIR_DEPTH`].
    pub async fn browse_workspace(
        &self,
        user_id: &str,
        conversation_id: &str,
        query: WorkspaceBrowseQuery,
    ) -> Result<Vec<WorkspaceEntry>, ConversationError> {
        if query.path.trim().is_empty() {
            return Err(ConversationError::BadRequest {
                reason: "path must not be empty".into(),
            });
        }

        let row = self
            .conversation_repo()
            .get(user_id, conversation_id)
            .await
            .map_err(|e| ConversationError::internal(format!("Failed to load conversation: {e}")))?
            .ok_or_else(|| ConversationError::NotFound {
                id: conversation_id.to_owned(),
            })?;

        let extra: serde_json::Value = serde_json::from_str(&row.extra)
            .map_err(|e| ConversationError::internal(format!("Invalid extra JSON: {e}")))?;
        let workspace = extra
            .get("workspace")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_owned();
        if workspace.is_empty() {
            return Err(ConversationError::BadRequest {
                reason: "Conversation has no workspace assigned".into(),
            });
        }

        let relative_path = query.path.trim_start_matches('/');
        let relative_path_obj = std::path::Path::new(relative_path);
        if relative_path_obj
            .components()
            .any(|component| matches!(component, Component::ParentDir))
        {
            return Err(ConversationError::BadRequest {
                reason: "Path traversal outside workspace is not allowed".into(),
            });
        }

        // Resolve the browsed path relative to the workspace root
        let base = std::path::Path::new(&workspace);
        let browse_path = if relative_path.is_empty() {
            base.to_path_buf()
        } else {
            base.join(relative_path_obj)
        };

        // Security: reject direct traversal outside the workspace root, but allow
        // symlinked directories mounted inside the workspace (e.g. native skill
        // dirs that point at the builtin skills corpus under data-dir).
        let canonical_base = base
            .canonicalize()
            .map_err(|e| ConversationError::internal(format!("Failed to resolve workspace path: {e}")))?;
        let canonical_browse = browse_path
            .canonicalize()
            .map_err(|_| ConversationError::not_found_reason("Directory not found"))?;
        if !browse_path.starts_with(base) && !canonical_browse.starts_with(&canonical_base) {
            return Err(ConversationError::BadRequest {
                reason: "Path traversal outside workspace is not allowed".into(),
            });
        }

        // Check depth limit
        let depth = relative_path_obj.components().count();
        if depth > MAX_DIR_DEPTH {
            return Err(ConversationError::BadRequest {
                reason: format!("Directory depth exceeds maximum of {MAX_DIR_DEPTH}"),
            });
        }

        let mut entries = Vec::new();
        let mut dir_reader = tokio::fs::read_dir(&canonical_browse)
            .await
            .map_err(|e| ConversationError::internal(format!("Failed to read directory: {e}")))?;

        while let Ok(Some(entry)) = dir_reader.next_entry().await {
            let name = entry.file_name().to_string_lossy().into_owned();

            // Apply search filter if provided
            if let Some(ref search) = query.search
                && !search.is_empty()
                && !name.to_lowercase().contains(&search.to_lowercase())
            {
                continue;
            }

            let entry_path = entry.path();
            let metadata = tokio::fs::metadata(&entry_path)
                .await
                .map_err(|e| ConversationError::internal(format!("Failed to read entry metadata: {e}")))?;

            let entry_type = if metadata.is_dir() { "directory" } else { "file" };

            entries.push(WorkspaceEntry {
                name,
                entry_type: entry_type.into(),
            });
        }

        // Sort: directories first, then alphabetically
        entries.sort_by(|a, b| {
            let type_cmp = a.entry_type.cmp(&b.entry_type);
            if type_cmp == std::cmp::Ordering::Equal {
                a.name.to_lowercase().cmp(&b.name.to_lowercase())
            } else {
                type_cmp
            }
        });

        Ok(entries)
    }
}

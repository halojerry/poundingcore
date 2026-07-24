-- Migration 032: Disable unsupported builtin ACP agents.
--
-- POUNDING only supports 4 core CLIs:
--   POUNDING CLI (agent_type='aionrs'), Claude Code, Hermes, OpenClaw.
-- All other builtin ACP agents (codex, opencode, gemini, qwen, kimi,
-- copilot, npx agents, binary agents, etc.) are disabled so they no
-- longer appear in the agent picker or consume detection resources.
--
-- Custom agents (agent_source='custom') and internal agents
-- (agent_type='aionrs') are NOT affected.
--
-- Existing DB instances that have already run 001-031 will have these
-- rows present; this migration disables them. Fresh installs will seed
-- all rows then immediately disable the unsupported ones via this
-- migration (it runs after 001-031 in order).

UPDATE agent_metadata
SET enabled = 0
WHERE agent_source = 'builtin'
  AND agent_type = 'acp'
  AND backend NOT IN ('claude', 'hermes', 'openclaw');

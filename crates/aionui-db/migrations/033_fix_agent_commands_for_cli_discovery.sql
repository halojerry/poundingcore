-- Migration 033: Fix agent commands for native CLI discovery
--
-- Hermes and OpenClaw ACP agents are seeded with command and
-- agent_source_info in migration 001/011, but these values are lost
-- during a full sqlx migration re-run. Reset them explicitly so the
-- agent availability probe can find the CLI binary on PATH.
--
-- Also ensures migration 032's disabled list is comprehensive:
-- disable EVERYTHING that isn't one of the 4 supported agents.

-- Fix Hermes native CLI metadata
UPDATE agent_metadata
SET command = 'hermes',
    args = '["acp"]',
    agent_source_info = json_set(COALESCE(agent_source_info, '{}'), '$.binary_name', 'hermes'),
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE id = '55f3ed1c';

-- Fix OpenClaw ACP metadata
UPDATE agent_metadata
SET command = 'openclaw',
    args = '["acp"]',
    agent_source_info = json_set(COALESCE(agent_source_info, '{}'), '$.binary_name', 'openclaw'),
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE id = 'b7e8a9c4';

-- Disable everything except our 4 supported agents
-- (POUNDING CLI / aionrs, Claude Code / claude, Hermes / hermes, OpenClaw / openclaw)
UPDATE agent_metadata SET enabled = 0,
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE agent_source = 'builtin'
  AND (
    (agent_type = 'acp' AND backend NOT IN ('claude', 'hermes', 'openclaw'))
    OR agent_type IN ('nanobot')
    OR (agent_type = 'openclaw-gateway' AND enabled = 1)
  );

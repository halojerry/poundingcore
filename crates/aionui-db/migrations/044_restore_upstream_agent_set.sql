-- Migration 044: Restore the upstream builtin ACP agent set.
--
-- Background: POUNDING's own migration 032 (`disable_unsupported_agents`)
-- restricted the builtin ACP agent set to 4 core CLIs (claude / hermes /
-- openclaw / aionrs) by flipping `enabled = 0` on every other builtin ACP
-- row:
--
--     UPDATE agent_metadata
--     SET enabled = 0
--     WHERE agent_source = 'builtin'
--       AND agent_type = 'acp'
--       AND backend NOT IN ('claude', 'hermes', 'openclaw');
--
-- 032 runs at migration version 32, BEFORE the upstream migrations that
-- reseed the full agent catalog (now 035 mimo-code, 037 omp, 040
-- antigravity, plus the base seed in 001). Those later INSERTs re-enabled
-- their own rows, but every other upstream agent (codex, gemini, qwen,
-- opencode, copilot, kimi, etc.) stayed disabled by 032.
--
-- Product decision: POUNDING now accepts the FULL upstream agent set
-- (codex / mimo-code / omp / antigravity included). The 032 restriction is
-- lifted and every builtin ACP agent returns to the upstream default
-- `enabled = 1`.
--
-- Safety & idempotency:
--   * 032 only ever toggled the `enabled` flag — it never deleted rows, so
--     the reverse is a pure UPDATE (no INSERT/DELETE needed).
--   * The `enabled = 0` guard confines this to rows 032 actually disabled.
--     Rows the user later manually re-enabled (or that were never disabled)
--     are left untouched, and user data (command/env overrides, handshake
--     fields, per-assistant toggles) is not modified.
--   * `custom` and `internal` agents never match 032's predicate and are
--     unaffected.
--   * Re-running the statement is a no-op (no disabled rows remain), which
--     keeps it idempotent.
--
-- The predicate mirrors 032 exactly (inverted to the restore direction).

UPDATE agent_metadata
SET enabled = 1
WHERE agent_source = 'builtin'
  AND agent_type = 'acp'
  AND enabled = 0
  AND backend NOT IN ('claude', 'hermes', 'openclaw');

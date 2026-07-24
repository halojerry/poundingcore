@AGENTS.md

# POUNDING AionCore — Development Guide

## Quick Commands

```bash
cargo check -p aionui-app                      # Full workspace type-check
cargo test -p aionui-<crate>                    # Test single crate
cargo test -p aionui-db                         # Migration validation
cargo test -p aionui-ai-agent                   # CC-Switch tests
cargo clippy -p aionui-<crate> -- -D warnings   # Lint single crate
cargo build --release -p aionui-app             # Release binary (poundingcore)
bash scripts/apply-branding.sh                  # Branding check (10 checks)
```

`cargo test --workspace` takes 10+ minutes — always use single-crate tests and `run_in_background: true`.

## Migration Numbering (Critical)

POUNDING has 3 exclusive migrations (013-015) and 3 renumbered migrations (028-030). Upstream's 022-027 are kept as-is.

| # | Owner | Name |
|---|-------|------|
| 013-015 | POUNDING | `add_pounding_cli`, `native_cli_managed_tools`, `add_pounding_cli` |
| 022-027 | Upstream | `cron_execution_dedup`, `add_pi_builtin_acp_agent`, … |
| 028-030 | POUNDING | `assistant_thought_level_defaults`, `update_codex_acp_package_scope`, `codex_agent_full_access_yolo_id` |

**When upstream adds migrations that conflict with POUNDING numbers**: rename the upstream files to the next available slot. Never renumber POUNDING-exclusive migrations downward.

## Branding

```bash
bash scripts/apply-branding.sh   # 10 checks — must pass before push
```

Key brand identity:
- Binary: `poundingcore` (not `aioncore`)
- DB file: `pounding-backend.db`
- Log prefix: `POUNDINGCORE_LISTENING`
- `DEFAULT_REPO` in `crates/aionui-system/src/version.rs`: `"halojerry/poundingcore"`

## Upstream Sync

### Standard merge

```bash
git checkout main
git merge upstream/main -X theirs --no-commit
# Renumber overlapping migrations if needed
bash scripts/apply-branding.sh
cargo check -p aionui-app
cargo test -p aionui-db
cargo test -p aionui-ai-agent
git add -A && git commit -m "merge: upstream vX.Y.Z"
git push origin main
```

### CC-Switch verification

After any merge touching `crates/aionui-ai-agent/src/factory/` or `session.rs`:
```bash
cargo test -p aionui-ai-agent
```
CC-Switch depends on correct ACP model config negotiation and provider resolution.

## Version Record

| 上游 | POUNDING 分支 | 结果 |
|------|-------------|------|
| v0.1.50 | main (poundingcore) | cargo check ✅, migrations 001-030 |
| v0.1.46 | feature/upstream-sync-v2.1.34 | cargo check ✅ |

# GSD Bridge

Bridge between GSD `*-PLAN.md` files and execution state/manifest artifacts used by Codex.

## Install

```bash
python3 -m pip install -e .
```

## CLI

```bash
gsd-bridge --version

# Export plans to Superpowers format + manifest + state files
gsd-bridge export .planning/phases/ --output-dir docs/plans
gsd-bridge export .planning/phases/ --pending          # only unexported plans
gsd-bridge export .planning/phases/ --dry-run          # preview without writing

# Quick status from manifest + live state files
gsd-bridge status docs/plans
gsd-bridge status docs/plans --json

# Post-execution reconciliation (drift, stale, missing verification) + STATUS.md
gsd-bridge reconcile docs/plans
gsd-bridge reconcile docs/plans --stale-hours 12

# Refresh manifest statuses from state files (no re-export)
gsd-bridge refresh docs/plans

# Validate a single plan file
gsd-bridge validate .planning/phases/01-foundation/01-01-PLAN.md

# Migrate all state files to current schema version
gsd-bridge migrate docs/plans

# Archive or remove plan artifacts
gsd-bridge archive docs/plans <plan_id>
gsd-bridge archive docs/plans <plan_id> --dry-run
gsd-bridge archive docs/plans <plan_id> --delete       # remove instead of archive
gsd-bridge archive docs/plans <plan_id> --force        # skip verified-status guard

# Codex adapter subprocess API
gsd-bridge adapter next-plan docs/plans/_manifest.json
gsd-bridge adapter start docs/plans/_state/<plan_id>.json
gsd-bridge adapter resume docs/plans/_state/<plan_id>.json
gsd-bridge adapter complete-task docs/plans/_state/<plan_id>.json <task_num>
gsd-bridge adapter record-verification docs/plans/_state/<plan_id>.json <tier> <command> <exit_code>
gsd-bridge adapter mark-verified docs/plans/_state/<plan_id>.json
gsd-bridge adapter mark-failed docs/plans/_state/<plan_id>.json --reason "msg"
gsd-bridge adapter mark-blocked docs/plans/_state/<plan_id>.json "reason" --severity critical
gsd-bridge adapter renew-lock docs/plans/_state/<plan_id>.json <run_id>
gsd-bridge adapter advance-step docs/plans/_state/<plan_id>.json <step>
gsd-bridge adapter rollback docs/plans/_manifest.json docs/plans/_state/<plan_id>.json
```

## Rollback Contract (Structured)

`execution_contract.rollback` must be structured JSON/YAML, not a raw shell string.

```xml
<execution_contract>
  <rollback>{"argv": ["git", "checkout", "--", "src/file.py"]}</rollback>
</execution_contract>
```

Supported fields:

- `argv` (required): command and arguments array
- `cwd` (optional): working directory
- `env` (optional): environment variable map

Rollback execution is shell-disabled and command-allowlisted. Override allowlist with:

```bash
export GSD_BRIDGE_ROLLBACK_ALLOWLIST="git,echo,python3"
```

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `GSD_BRIDGE_PLANS_DIR` | Override inferred plans directory path |
| `GSD_BRIDGE_PROJECT_ROOT` | Override inferred project root path |
| `GSD_BRIDGE_ROLLBACK_ALLOWLIST` | Comma-separated list of allowed rollback executables (overrides defaults) |

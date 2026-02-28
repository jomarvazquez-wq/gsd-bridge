Export pending GSD plans to Superpowers format, generating manifest and state files.

## Usage

- `/bridge:export` — exports all pending plans from `.planning/` (default)
- `/bridge:export path/to/plan.md` — exports a specific plan file
- `/bridge:export .planning/phases/14-product-finder-i18n/` — exports from a specific phase directory

## Command

```bash
python -m gsd_bridge.cli export $ARGUMENTS --pending
```

If no `$ARGUMENTS` were provided, the CLI defaults to `.planning/` automatically.

## What to expect

For each plan file processed:
- `Processing: <filename>` on stderr
- `-> <output_path>` for each exported file
- Final summary: Exported count, Skipped (unchanged) count, Invalid count with details

Output files land in `docs/plans/`:
- `_manifest.json` — central registry of all plans
- `_state/<plan_id>.json` — per-plan execution state (initially "pending")
- `<plan_id>.md` — Superpowers-format plan

If you see "No PLAN.md files found" — there are no pending plans (plans without a matching SUMMARY.md).

## Follow-up commands

- `/bridge:export-dry` to preview without writing files
- `/bridge:status` to check the resulting plan statuses
- `/bridge:validate` to validate pending plans before export

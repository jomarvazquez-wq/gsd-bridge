Show bridge plan status as machine-readable JSON.

Run this command:

```bash
python -m gsd_bridge.cli status --json
```

## What to expect

JSON object with keys:
- `manifest`: path to manifest file
- `plans_dir`: path to plans directory
- `summary`: object with counts (total, verified, pending, executing, blocked, failed, no-state)
- `plans`: array of plan objects, each with plan_id, status, tasks (completed/total), wave

If you see "No manifest" — no plans have been exported yet. Run `/bridge:export` first.

## Follow-up commands

- `/bridge:status` for human-readable table
- `/bridge:blocked` to inspect blocked plans

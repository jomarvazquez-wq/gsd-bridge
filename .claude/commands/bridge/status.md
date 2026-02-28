Show the current status of all bridge plans in a formatted table.

Run this command:

```bash
python -m gsd_bridge.cli status
```

## What to expect

A table with columns: Plan ID, Status, Tasks (completed/total), Wave.
Below the table: summary counts (Total, Verified, Pending, Executing, Blocked, Failed, No-state).

If you see "No manifest" — no plans have been exported yet. Run `/bridge:export` first.

## Follow-up commands

- `/bridge:status-json` for machine-readable output
- `/bridge:blocked` to see details on blocked plans
- `/bridge:reconcile` to detect drift and stale executions

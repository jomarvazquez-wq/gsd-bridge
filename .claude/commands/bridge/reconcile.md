Run post-execution reconciliation: detect drift, stale executions, and sync manifest.

Run this command:

```bash
python -m gsd_bridge.cli reconcile
```

## What to expect

- Generates `docs/plans/STATUS.md` with a full reconciliation report.
- Syncs manifest statuses from live state files.
- Console summary: Total, Verified, Pending, Executing, Blocked, Failed, No-state counts.

Reconciliation detects:
- **Drift**: source plan files modified after export (content hash mismatch)
- **Stale executions**: plans in `executing` state for longer than 24 hours (default threshold)
- **Missing verifications**: completed plans that haven't recorded verification results

## Common flags

- `--stale-hours <N>` to change the stale threshold (default: 24)

## Follow-up commands

- `/bridge:status` to see the refreshed status table
- `/bridge:blocked` to investigate blocked plans
- `/bridge:export` to re-export drifted plans

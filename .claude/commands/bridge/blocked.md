Show all blocked plans with their blocker details.

Run this command:

```bash
python -m gsd_bridge.cli blocked
```

## What to expect

A table with columns: Plan ID, Severity, Reason.
If no plans are blocked: "No blocked plans."

For detailed blocker info (severity, who must answer, missing info, resume commands), blocker files live at `docs/plans/_blockers/<plan_id>.md`.

## Follow-up commands

- `/bridge:blocked-json` for machine-readable output
- `/bridge:resume <plan_id>` after fixing the blocker
- `/bridge:unlock <plan_id>` if the plan is stuck with a stale lease (rare)
- `/bridge:status` for the full status table

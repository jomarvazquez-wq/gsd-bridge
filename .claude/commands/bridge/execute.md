Get the next eligible plan and start its execution.

Run this command:

```bash
python -m gsd_bridge.cli execute
```

## What to expect

- If a plan is eligible: "Started: <plan_id> (wave N)" — the plan transitions to `executing` with a fresh lease.
- If no plans are eligible: "No eligible plans to execute." — all plans are either already executing, verified, blocked, or waiting on wave/dependency ordering.

The command respects wave ordering and `depends_on` dependencies automatically.

## Common flags

- `--plan <plan_id>` to target a specific plan
- `--wave <N>` to only start plans in that wave
- `--max-plans <N>` to start multiple plans (default: 1)
- `--dry-run` to preview what would execute without starting

## Follow-up commands

- `/bridge:status` to monitor progress
- `/bridge:blocked` to check if any plans are stuck
- `/bridge:reconcile` after execution completes

Resume a blocked or failed plan back to executing state.

**Usage**: `/bridge:resume <plan_id>`

## Before resuming

Make sure the blocker has been resolved. Check what was blocking:

```bash
cat docs/plans/_blockers/$ARGUMENTS.md
```

If the blocker file describes missing information or a required action, handle that first. Resuming without fixing the root cause will likely re-block.

## Command

```bash
python -m gsd_bridge.cli resume $ARGUMENTS --yes
```

## What to expect

- On success: "Resumed: <plan_id> -> executing"
- On error: "Resume requires blocked/failed state" — the plan is not in a resumable state. Check `/bridge:status` for its current status.

## Follow-up commands

- `/bridge:status` to verify the plan is now executing
- `/bridge:blocked` to check for other blocked plans

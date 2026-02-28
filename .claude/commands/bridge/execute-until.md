Execute plans sequentially until one becomes blocked.

Run this command:

```bash
python -m gsd_bridge.cli execute --until blocked --max-plans 100
```

## Safety note

This command starts multiple plans in sequence. Each plan acquires its own lease. Already-started plans remain in `executing` state and can be managed with `/bridge:status`.

## What to expect

- Plans are started one at a time in wave/dependency order.
- Loop stops when: no more eligible plans, or a plan becomes blocked.
- Reports total plans started.

## Follow-up commands

- `/bridge:blocked` to see blocker details
- `/bridge:resume <plan_id>` after fixing the blocker
- `/bridge:status` to see overall progress

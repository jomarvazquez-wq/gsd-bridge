Show blocked plans as machine-readable JSON.

Run this command:

```bash
python -m gsd_bridge.cli blocked --json
```

## What to expect

JSON array of blocked plan objects, each with:
- `plan_id`: the plan identifier
- `severity`: critical, high, medium, or low
- `reason`: why the plan is blocked
- `blocker_file`: full content of the blocker markdown file
- `wave`: the plan's wave number

If no plans are blocked: "No blocked plans." on stderr (empty stdout).

## Follow-up commands

- `/bridge:blocked` for human-readable table
- `/bridge:resume <plan_id>` after fixing the blocker

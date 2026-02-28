Force-release a stuck lease on a plan and resume execution.

**Usage**: `/bridge:unlock <plan_id>`

## Safety warning

This command is **rare** and should only be used when a plan is genuinely stuck — e.g., the executor process died mid-execution and the lease hasn't expired naturally. In most cases, `/bridge:resume` is sufficient after fixing a blocker.

Do NOT use unlock as a shortcut to skip blockers. Address the root cause first.

## Command

```bash
python -m gsd_bridge.cli unlock $ARGUMENTS --force --yes
```

## What to expect

- If executing: marks failed (releases lease), then immediately resumes with a fresh lease.
- If blocked/failed: directly resumes.
- The audit trail preserves the unlock event in `recovery_notes`.

## When to use unlock vs resume

| Scenario | Use |
|----------|-----|
| Plan is `blocked` and blocker is fixed | `/bridge:resume` |
| Plan is `failed` and you want to retry | `/bridge:resume` |
| Plan is `executing` but the executor died | `/bridge:unlock` |
| Lease expired naturally | `/bridge:resume` (lease auto-released) |

## Follow-up commands

- `/bridge:status` to verify the plan is now executing
- `/bridge:reconcile` to detect other stale executions

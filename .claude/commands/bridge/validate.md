Validate all pending GSD plan files (plans without a matching SUMMARY.md).

Run this command:

```bash
python -m gsd_bridge.cli validate --pending
```

## What to expect

For each pending plan:
- `OK:   <filename>` on success
- `FAIL: <filename>` on failure, with specific error details

Summary line: `Validated: N | Passed: N | Failed: N`

If no pending plans found: "No pending plans found."

## Follow-up commands

- `/bridge:export` to export validated plans
- `/bridge:export-dry` to preview the export

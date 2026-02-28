Preview pending plan exports without writing any files.

Run this command:

```bash
python -m gsd_bridge.cli export --pending --dry-run
```

## What to expect

For each pending plan, prints the Superpowers-format markdown that would be written, framed by separator lines. No files are created or modified.

This is useful to:
- Verify plan parsing before committing to export
- Review the converted Superpowers format
- Check which plans would be included

## Follow-up commands

- `/bridge:export` to perform the actual export
- `/bridge:validate` to validate plans before export

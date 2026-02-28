Start execution for all pending plans in a specific wave.

**Usage**: `/bridge:execute-wave <wave_number>` (e.g., `/bridge:execute-wave 14`)

Run this command:

```bash
python -m gsd_bridge.cli execute --wave $ARGUMENTS --max-plans 100
```

## What to expect

- Plans in lower waves must be `verified` before higher-wave plans can start. If no plans match, earlier waves may still be in progress.
- Each started plan transitions to `executing` with a fresh lease.
- Reports how many plans were started.

## Follow-up commands

- `/bridge:status` to monitor wave progress
- `/bridge:execute` to start the next eligible plan regardless of wave

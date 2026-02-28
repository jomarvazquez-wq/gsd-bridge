# Test Fixtures

This directory contains fixtures for regression tests.

## Pattern

Every bug fix should produce:
1. A fixture file here (e.g., `legacy_v2_state_no_cursor.json`)
2. A test in the relevant test file that reads the fixture and asserts the fix

## Example

```python
import json
from pathlib import Path

def test_regression_missing_cursor_field(self):
    fixture = Path(__file__).parent / "fixtures" / "legacy_v2_state_no_cursor.json"
    # Write fixture content to a temp state file, then exercise the code path
    state_path.write_text(fixture.read_text(), encoding="utf-8")
    state = read_state(state_path)
    self.assertIsNone(state.cursor)  # must not raise
```

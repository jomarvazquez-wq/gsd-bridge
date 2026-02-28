"""GSD Bridge exception hierarchy.

Three categories for quick error triage:

    InputError       — bad user input, wrong file path, invalid JSON
    LogicError       — internal invariant violation, bad state transition
    IntegrationError — external I/O failure, subprocess error, lock timeout

All inherit from GSDError for a single catch-all if needed.
"""

from __future__ import annotations


class GSDError(Exception):
    """Base class for all GSD Bridge errors."""

    category: str = "unknown"


class InputError(GSDError):
    """Bad user-supplied input: missing file, invalid argument, malformed JSON."""

    category = "input"


class LogicError(GSDError):
    """Internal invariant violated: bad state transition, schema mismatch."""

    category = "logic"


class IntegrationError(GSDError):
    """External I/O failure: lock timeout, subprocess error, file write failure."""

    category = "integration"

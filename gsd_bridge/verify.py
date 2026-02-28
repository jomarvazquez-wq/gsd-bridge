"""Verification tier classification.

Heuristically classifies verify commands from GSD plans into three tiers:
  - quick:  grep, tsc, lint — fast checks
  - full:   build, test suites — comprehensive checks
  - smoke:  runtime checks, lighthouse, curl — requires running server
"""

from __future__ import annotations

import re

# Patterns that indicate a quick verification (seconds, no build)
QUICK_PATTERNS: list[str] = [
    r"\bgrep\b",
    r"\btsc\b",
    r"\blint\b",
    r"\beslint\b",
    r"\bprettier\b",
    r"\brg\b",          # ripgrep
    r"Verify.*import",  # "Verify X imports Y" — visual/grep check
    r"Verify.*exist",   # "Verify file exists"
    r"Check.*import",
]

# Patterns that indicate a smoke test (needs running server or browser)
SMOKE_PATTERNS: list[str] = [
    r"\blocalhost\b",
    r"\blighthouse\b",
    r"\bcurl\b",
    r"\bvisual\b",
    r"\bbrowser\b",
    r"\bdevtools\b",
    r"\bvisually\b",
    r"\bmanually\b",
    r"open\s+http",
    r"Run\s+Lighthouse",
    r"Chrome\s+DevTools",
    r"npm\s+run\s+dev\b",
    r"next\s+start\b",
]

# Everything else defaults to "full" (build, test, comprehensive check)


def parse_verify_tiers(verify_text: str) -> dict[str, list[str]]:
    """Classify verification commands into quick/full/smoke tiers.

    Splits on numbered items or newlines, then classifies each line.
    Returns only non-empty tiers.
    """
    if not verify_text or not verify_text.strip():
        return {}

    tiers: dict[str, list[str]] = {"quick": [], "full": [], "smoke": []}

    # Split on numbered items (1. 2. 3.) or plain newlines
    lines = re.split(r"\n(?=\d+[\.\)]\s)", verify_text.strip())
    if len(lines) == 1:
        # No numbered items — split on newlines
        lines = verify_text.strip().splitlines()

    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            continue

        # Strip leading numbers/bullets
        cleaned_for_match = re.sub(r"^\d+[\.\)]\s*", "", cleaned)
        cleaned_for_match = re.sub(r"^[-*]\s*", "", cleaned_for_match)

        if not cleaned_for_match.strip():
            continue

        tier = _classify_line(cleaned_for_match)
        tiers[tier].append(cleaned)

    return {k: v for k, v in tiers.items() if v}


def classify_task_verify(verify_text: str) -> dict[str, list[str]]:
    """Classify a single task's verify block into tiers.

    Similar to parse_verify_tiers but designed for per-task verify blocks
    which are typically shorter (1-3 lines).
    """
    if not verify_text or not verify_text.strip():
        return {}

    tiers: dict[str, list[str]] = {"quick": [], "full": [], "smoke": []}

    for line in verify_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        tier = _classify_line(line)
        tiers[tier].append(line)

    return {k: v for k, v in tiers.items() if v}


def _classify_line(line: str) -> str:
    """Classify a single verification line into a tier."""
    for pattern in QUICK_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return "quick"

    for pattern in SMOKE_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return "smoke"

    return "full"

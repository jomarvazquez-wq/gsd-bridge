"""Stable plan ID generation and content hashing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def generate_plan_id(source_path: Path, content: str) -> str:
    """Generate stable plan ID: slug(phase-plan) + short SHA-256.

    Examples:
        .planning/phases/02-homepage-trust-signals/02-01-PLAN.md
        → "02-homepage-trust-signals-01-a3f8c2d"

        .planning/phases/08-chatbot-integration-launch-polish/08-04-PLAN.md
        → "08-chatbot-integration-launch-polish-04-b7e1f09"
    """
    slug = extract_slug_from_path(source_path)
    short = hashlib.sha256(content.encode()).hexdigest()[:7]
    return f"{slug}-{short}"


def extract_slug_from_path(source_path: Path) -> str:
    """Extract human-readable slug from a GSD plan path.

    Looks for the pattern: phases/<phase-name>/<NN-MM-PLAN.md>
    Returns: "<phase-name>-<MM>"
    """
    parts = source_path.parts
    # Find the "phases" segment and take the directory after it
    phase_dir = None
    plan_file = source_path.name

    for i, part in enumerate(parts):
        if part == "phases" and i + 1 < len(parts):
            phase_dir = parts[i + 1]
            break

    if not phase_dir:
        # Fallback: use parent directory name
        phase_dir = source_path.parent.name

    # Extract plan number from filename: "02-01-PLAN.md" → "01"
    plan_match = re.match(r"\d+-(\d+)-PLAN\.md", plan_file)
    plan_num = plan_match.group(1) if plan_match else "00"

    return f"{phase_dir}-{plan_num}"


def content_hash(content: str) -> str:
    """Full SHA-256 of plan content for drift detection."""
    return hashlib.sha256(content.encode()).hexdigest()

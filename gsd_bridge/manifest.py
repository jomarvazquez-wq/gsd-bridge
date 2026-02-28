"""Manifest generation — the ordered plan list that Codex uses as its entrypoint.

docs/plans/_manifest.json contains every exported plan sorted by
wave → phase → plan number, with status mirrored from state files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .plan_id import content_hash, generate_plan_id
from .schemas import Manifest, ManifestEntry
from .state import init_state, read_state, write_state


def generate_manifest(
    parsed_plans: list[dict[str, Any]],
    output_dir: Path,
    project_root: Path,
) -> Manifest:
    """Build an ordered manifest from parsed plans + existing/new state files.

    For each plan:
      1. Generate plan_id from source path + content
      2. Read existing state file or create one as pending
      3. Build a ManifestEntry with status mirrored from state
      4. Sort by priority (wave * 1000 + phase_num * 10 + plan_num)
    """
    state_dir = output_dir / "_state"
    entries: list[ManifestEntry] = []

    for parsed in parsed_plans:
        raw_content = parsed["raw_content"]
        source_path = Path(parsed["source_path"])
        plan_id = generate_plan_id(source_path, raw_content)
        source_hash = content_hash(raw_content)
        fm = parsed["frontmatter"]
        depends_raw = fm.get("depends_on", [])
        if isinstance(depends_raw, list):
            depends_on = [str(d) for d in depends_raw]
        elif depends_raw:
            depends_on = [str(depends_raw)]
        else:
            depends_on = []

        # Read or init state
        state_path = state_dir / f"{plan_id}.json"
        state = read_state(state_path)
        if state is None:
            state = init_state(
                plan_id=plan_id,
                source_path=str(source_path.relative_to(project_root))
                if source_path.is_relative_to(project_root)
                else str(source_path),
                source_hash=source_hash,
                total_tasks=len(parsed["tasks"]),
            )
            write_state(state_path, state)

        # Compute priority for sorting
        phase_num = _extract_phase_number(fm.get("phase", "00"))
        plan_num = int(fm.get("plan", 0))
        wave = int(fm.get("wave", 1))
        priority = wave * 1000 + phase_num * 10 + plan_num

        # Relative paths for manifest
        try:
            plan_path_rel = str(
                (output_dir / f"{plan_id}.md").relative_to(project_root)
            )
        except ValueError:
            plan_path_rel = f"docs/plans/{plan_id}.md"

        try:
            state_path_rel = str(state_path.relative_to(project_root))
        except ValueError:
            state_path_rel = f"docs/plans/_state/{plan_id}.json"

        try:
            source_path_rel = str(source_path.relative_to(project_root))
        except ValueError:
            source_path_rel = str(source_path)

        entry = ManifestEntry(
            plan_id=plan_id,
            wave=wave,
            phase=fm.get("phase", "unknown"),
            plan_number=plan_num,
            priority=priority,
            plan_path=plan_path_rel,
            state_path=state_path_rel,
            source_path=source_path_rel,
            source_hash=source_hash,
            depends_on=depends_on,
            batch_size=int(fm.get("batch_size", 3)),
            batching=fm.get("batching"),
            status=state.status,
            execution_contract=(
                parsed.get("execution_contract")
                if parsed.get("execution_contract")
                else None
            ),
        )
        entries.append(entry)

    # Sort by priority
    entries.sort(key=lambda e: e.priority)

    manifest = Manifest(
        project_root=str(project_root),
        plans=entries,
    )
    manifest.compute_summary()

    return manifest


def write_manifest(manifest: Manifest, output_path: Path) -> None:
    """Write manifest JSON to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(manifest.to_json() + "\n", encoding="utf-8")


def read_manifest(manifest_path: Path) -> Manifest:
    """Read manifest from JSON file."""
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read manifest: {manifest_path}") from exc

    try:
        return Manifest.from_json(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid manifest JSON at {manifest_path}: {exc}") from exc
    except TypeError as exc:
        raise ValueError(f"Invalid manifest schema at {manifest_path}: {exc}") from exc


def _extract_phase_number(phase: str) -> int:
    """Extract leading number from phase name: '02-homepage' → 2."""
    match = re.match(r"(\d+)", phase)
    return int(match.group(1)) if match else 0

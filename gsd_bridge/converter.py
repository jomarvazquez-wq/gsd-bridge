"""Convert parsed GSD plans to Superpowers-compatible markdown.

Upgraded from the original gsd_to_superpowers.py with:
  - Stable plan IDs (not date-prefixed filenames)
  - Verification tiers (quick/full/smoke)
  - Plan-specified batch sizes
  - Execution contract sections
  - Manifest + state file references in execution notes
"""

from __future__ import annotations

import json
import re
from typing import Any

from .plan_id import content_hash
from .verify import classify_task_verify, parse_verify_tiers


def convert_to_superpowers(
    parsed: dict[str, Any],
    plan_id: str,
    plans_dir: str = "docs/plans",
) -> str:
    """Convert a parsed GSD plan into superpowers-compatible markdown."""
    fm = parsed["frontmatter"]
    phase = fm.get("phase", "unknown")
    plan_num = fm.get("plan", "?")
    files_modified = fm.get("files_modified", [])
    must_haves = fm.get("must_haves", {})
    truths = must_haves.get("truths", [])
    wave = fm.get("wave", 1)
    depends_raw = fm.get("depends_on", [])
    if isinstance(depends_raw, list):
        depends_on = [str(d) for d in depends_raw]
    elif depends_raw:
        depends_on = [str(depends_raw)]
    else:
        depends_on = []
    source_hash = content_hash(parsed["raw_content"])
    batch_size = fm.get("batch_size", 3)
    batching = fm.get("batching", None)
    execution_contract = parsed.get("execution_contract")
    plans_dir = plans_dir.rstrip("/") or "docs/plans"

    lines: list[str] = []

    # --- Header ---
    lines.append(f"# Plan: {phase} (Plan {plan_num})")
    lines.append("")
    lines.append(f"> Plan ID: `{plan_id}`")
    lines.append(f"> Source: `{parsed['source_path']}`")
    lines.append(f"> Source hash: `{source_hash[:12]}...`")
    lines.append("")

    # --- Metadata table ---
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Plan ID | `{plan_id}` |")
    lines.append(f"| Phase | `{phase}` |")
    lines.append(f"| Plan | {plan_num} |")
    lines.append(f"| Wave | {wave} |")
    if depends_on:
        lines.append(f"| Dependencies | {', '.join(str(d) for d in depends_on)} |")
    else:
        lines.append("| Dependencies | None |")
    lines.append(f"| Total tasks | {len(parsed['tasks'])} |")
    lines.append(f"| Batch size | {batch_size} |")
    lines.append(f"| State file | `{plans_dir}/_state/{plan_id}.json` |")
    lines.append("")

    # --- Execution Contract ---
    if execution_contract:
        lines.append("## Execution Contract")
        lines.append("")
        if execution_contract.get("inputs"):
            lines.append("**Inputs (prerequisites):**")
            lines.append(execution_contract["inputs"])
            lines.append("")
        if execution_contract.get("outputs"):
            lines.append("**Outputs (expected changes):**")
            lines.append(execution_contract["outputs"])
            lines.append("")
        if execution_contract.get("side_effects"):
            lines.append("**Side effects:**")
            lines.append(execution_contract["side_effects"])
            lines.append("")
        if execution_contract.get("rollback"):
            lines.append("**Rollback:**")
            rollback = execution_contract["rollback"]
            if isinstance(rollback, (dict, list)):
                lines.append("```json")
                lines.append(json.dumps(rollback, indent=2))
                lines.append("```")
            else:
                lines.append(str(rollback))
            lines.append("")

    # --- Objective ---
    lines.append("## Objective")
    lines.append("")
    lines.append(parsed["objective"])
    lines.append("")

    # --- Files involved ---
    if files_modified:
        lines.append("## Files Involved")
        lines.append("")
        for f in files_modified:
            lines.append(f"- `{f}`")
        lines.append("")

    # --- Success criteria (from must_haves.truths) ---
    if truths:
        lines.append("## Success Criteria")
        lines.append("")
        for truth in truths:
            lines.append(f"- [ ] {truth}")
        lines.append("")

    # --- Tasks ---
    lines.append("---")
    lines.append("")
    lines.append("## Tasks")
    lines.append("")

    task_batches = _compute_batches(len(parsed["tasks"]), batch_size, batching)
    total_batches = len(task_batches)

    for i, task in enumerate(parsed["tasks"], 1):
        batch_num = _task_batch_number(i, task_batches)
        task_type = task.get("type", "auto")
        gate = task.get("gate")

        header = f"### Task {i}: {_strip_task_prefix(task['name'])}"
        if task_type.startswith("checkpoint"):
            header += f" [{task_type}]"
            if gate:
                header += f" (gate: {gate})"
        lines.append(header)
        lines.append("")

        lines.append(f"*Batch {batch_num} of {total_batches}*")
        lines.append("")

        if task["files"]:
            lines.append(f"**Files:** {task['files']}")
            lines.append("")

        lines.append("**Steps:**")
        lines.append("")
        lines.append(task["action"].strip())
        lines.append("")

        # Per-task verification with tiers
        if task["verify"]:
            task_tiers = classify_task_verify(task["verify"])
            if task_tiers:
                for tier_name in ("quick", "full", "smoke"):
                    tier_items = task_tiers.get(tier_name, [])
                    if tier_items:
                        lines.append(f"**Verify ({tier_name.title()}):**")
                        lines.append("")
                        for item in tier_items:
                            lines.append(item)
                        lines.append("")
            else:
                lines.append("**Verification:**")
                lines.append("")
                lines.append(task["verify"])
                lines.append("")

        if task["done"]:
            lines.append("**Done when:**")
            lines.append("")
            lines.append(f"> {task['done']}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # --- Final verification checklist with tiers ---
    if parsed["verification"]:
        tiers = parse_verify_tiers(parsed["verification"])
        if tiers:
            lines.append("## Final Verification")
            lines.append("")
            for tier_name in ("quick", "full", "smoke"):
                tier_items = tiers.get(tier_name, [])
                if tier_items:
                    lines.append(f"### Verify ({tier_name.title()})")
                    lines.append("")
                    for item in tier_items:
                        lines.append(f"- [ ] {item}")
                    lines.append("")
        else:
            lines.append("## Final Verification Checklist")
            lines.append("")
            lines.append(parsed["verification"])
            lines.append("")

    # --- Completion criteria ---
    if parsed["success_criteria"]:
        lines.append("## Completion Criteria")
        lines.append("")
        lines.append(parsed["success_criteria"])
        lines.append("")

    # --- Execution notes ---
    lines.append("---")
    lines.append("")
    lines.append("## Execution Notes (for Codex/Superpowers)")
    lines.append("")
    lines.append(f"- **Manifest entrypoint**: `{plans_dir}/_manifest.json`")
    lines.append(f"- **State file**: `{plans_dir}/_state/{plan_id}.json`")
    lines.append("- Before starting, update state file: `status` → `executing`")
    lines.append("- After each task, update `completed_tasks` in state file")
    lines.append(f"- Execute tasks in batches of {batch_size}, then pause for review")
    lines.append("- Each task should be committed separately")
    lines.append("- Run verification commands after each task (quick first, then full)")
    lines.append(f"- If blocked: set `status` → `blocked`, write blocker to `{plans_dir}/_blockers/`")
    lines.append("- If all verification passes: set `status` → `verified`")
    lines.append("- Do not proceed past a failing verification step")
    lines.append("")

    return "\n".join(lines)


def _strip_task_prefix(name: str) -> str:
    """Remove 'Task N: ' prefix if present since we add our own numbering."""
    return re.sub(r"^Task\s+\d+:\s*", "", name)


def _compute_batches(
    total_tasks: int,
    batch_size: int,
    custom_batching: list[list[int]] | None,
) -> list[list[int]]:
    """Compute task batches from custom groups or uniform batch_size."""
    if custom_batching:
        return custom_batching

    batches: list[list[int]] = []
    for start in range(1, total_tasks + 1, batch_size):
        end = min(start + batch_size, total_tasks + 1)
        batches.append(list(range(start, end)))
    return batches


def _task_batch_number(task_num: int, batches: list[list[int]]) -> int:
    """Find which batch a task belongs to (1-indexed)."""
    for i, batch in enumerate(batches, 1):
        if task_num in batch:
            return i
    return len(batches)  # fallback

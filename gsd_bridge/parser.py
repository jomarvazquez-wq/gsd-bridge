"""GSD PLAN.md file parsing.

Extracted from the original gsd_to_superpowers.py with additions for
execution contracts and verification tier parsing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from a GSD PLAN.md file."""
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not match:
        return {}, text
    frontmatter = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)
    return frontmatter, body


def extract_tag(body: str, tag: str) -> str:
    """Extract content between <tag>...</tag> from GSD plan body."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, body, re.DOTALL)
    return match.group(1).strip() if match else ""


def extract_tasks(body: str) -> list[dict[str, Any]]:
    """Extract all <task> blocks from the <tasks> section."""
    tasks_block = extract_tag(body, "tasks")
    if not tasks_block:
        return []

    tasks: list[dict[str, Any]] = []
    # Match both <task type="auto"> and <task type="checkpoint:human-verify" gate="blocking">
    task_pattern = r'<task\s+[^>]*?type="([^"]+)"[^>]*>(.*?)</task>'
    for match in re.finditer(task_pattern, tasks_block, re.DOTALL):
        task_type = match.group(1)
        task_body = match.group(2)

        # Extract gate attribute if present
        gate_match = re.search(r'gate="([^"]+)"', match.group(0))
        gate = gate_match.group(1) if gate_match else None

        tasks.append({
            "type": task_type,
            "gate": gate,
            "name": extract_tag(task_body, "name"),
            "files": extract_tag(task_body, "files"),
            "action": extract_tag(task_body, "action"),
            "verify": extract_tag(task_body, "verify"),
            "done": extract_tag(task_body, "done"),
        })
    return tasks


def parse_execution_contract(body: str) -> dict[str, Any] | None:
    """Extract <execution_contract> block if present.

    Expected sub-tags:
        <inputs>env vars, secrets, required services</inputs>
        <outputs>files expected to change</outputs>
        <side_effects>migrations, external calls</side_effects>
        <rollback>minimum rollback approach</rollback>
    """
    contract_text = extract_tag(body, "execution_contract")
    if not contract_text:
        return None

    rollback_raw = extract_tag(contract_text, "rollback")

    return {
        "inputs": extract_tag(contract_text, "inputs") or None,
        "outputs": extract_tag(contract_text, "outputs") or None,
        "side_effects": extract_tag(contract_text, "side_effects") or None,
        "rollback": _parse_rollback_contract(rollback_raw),
    }


def _parse_rollback_contract(rollback_text: str) -> Any:
    """Parse rollback content into structured contract shape when possible."""
    stripped = rollback_text.strip()
    if not stripped:
        return None

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(stripped)
        except yaml.YAMLError:
            return stripped

    if parsed is None:
        return None
    return parsed


def parse_gsd_plan(plan_path: Path) -> dict[str, Any]:
    """Parse a complete GSD PLAN.md file into a structured dict."""
    text = plan_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)

    return {
        "source_path": str(plan_path),
        "raw_content": text,
        "frontmatter": frontmatter,
        "objective": extract_tag(body, "objective"),
        "context": extract_tag(body, "context"),
        "tasks": extract_tasks(body),
        "verification": extract_tag(body, "verification"),
        "success_criteria": extract_tag(body, "success_criteria"),
        "execution_contract": parse_execution_contract(body),
    }


def validate_plan(parsed: dict[str, Any], require_contract: bool = False) -> list[str]:
    """Validate a parsed plan has required fields. Returns list of error messages."""
    errors: list[str] = []
    fm = parsed.get("frontmatter", {})

    if not fm.get("phase"):
        errors.append("Missing frontmatter field: phase")
    if fm.get("plan") is None:
        errors.append("Missing frontmatter field: plan")
    if not parsed.get("objective"):
        errors.append("Missing <objective> tag")
    if not parsed.get("tasks"):
        errors.append("No <task> blocks found in <tasks>")
    if not parsed.get("verification"):
        errors.append("Missing <verification> tag")
    if not parsed.get("success_criteria"):
        errors.append("Missing <success_criteria> tag")

    if require_contract and not parsed.get("execution_contract"):
        errors.append(
            "Missing <execution_contract> block "
            "(required: inputs, outputs, side_effects, rollback)"
        )
    contract = parsed.get("execution_contract")
    if contract:
        rollback = contract.get("rollback")
        if rollback is not None and not _is_structured_rollback(rollback):
            errors.append(
                "execution_contract.rollback must be structured JSON/YAML "
                "(argv array or object with argv)"
            )

    return errors


def _is_structured_rollback(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value) and all(isinstance(item, str) and item.strip() for item in value)
    if isinstance(value, dict):
        argv = value.get("argv")
        return isinstance(argv, list) and bool(argv) and all(
            isinstance(item, str) and item.strip() for item in argv
        )
    return False

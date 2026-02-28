"""CLI with subcommands for the GSD Bridge."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from . import codex_adapter
from .converter import convert_to_superpowers
from .exceptions import GSDError
from .log import configure_logging, get_logger, new_run_id
from .manifest import generate_manifest, read_manifest, write_manifest
from .parser import parse_gsd_plan, validate_plan
from .plan_id import content_hash, generate_plan_id
from .reconcile import STALE_THRESHOLD_HOURS, generate_status_md, reconcile
from .schemas import CURRENT_SCHEMA_VERSION
from .state import read_state, write_state


DEPLOY_MARKER = ".gsd_bridge_deployed"


def find_plan_files(path: Path, pending_only: bool = False) -> list[Path]:
    """Find GSD PLAN.md files from a path (file, phase dir, or .planning dir)."""
    if path.is_file() and path.name.endswith("-PLAN.md"):
        return [path]

    plans = sorted(path.rglob("*-PLAN.md"))
    if pending_only:
        plans = [
            p
            for p in plans
            if not p.with_name(p.name.replace("-PLAN.md", "-SUMMARY.md")).exists()
        ]
    return plans


def _find_project_root(input_path: Path) -> Path:
    """Find project root from a .planning parent, else fall back to input path."""
    resolved = input_path.resolve()
    probe = resolved if resolved.is_dir() else resolved.parent

    for parent in [probe] + list(probe.parents):
        if parent.name == ".planning":
            return parent.parent
        if (parent / ".planning").is_dir():
            return parent

    return probe


def _check_strict_contract(parsed: dict[str, Any]) -> list[str]:
    """Check execution contract completeness for --strict mode."""
    contract = parsed.get("execution_contract")
    if not contract:
        return ["execution_contract is missing"]
    missing = [f for f in ("inputs", "outputs", "side_effects", "rollback") if not contract.get(f)]
    return [f"execution_contract missing fields: {', '.join(missing)}"] if missing else []


def _is_new_plan(plan_path: Path, project_root: Path) -> bool:
    """Determine if a plan is "new" (created after bridge v2 deployment)."""
    marker = project_root / DEPLOY_MARKER
    if not marker.exists():
        return False
    marker_mtime = marker.stat().st_mtime
    return plan_path.stat().st_mtime > marker_mtime


def _render_plans_dir(output_dir: Path, project_root: Path) -> str:
    """Render plans directory path for markdown/docs output."""
    try:
        return str(output_dir.relative_to(project_root))
    except ValueError:
        return str(output_dir)


def _blocked_plan_ids(plans_dir: Path) -> list[str]:
    """Return blocked plan IDs based on current state files."""
    state_dir = plans_dir / "_state"
    blocked_ids: list[str] = []
    for state_file in sorted(state_dir.glob("*.json")):
        state = read_state(state_file)
        if state and state.status == "blocked":
            blocked_ids.append(state.plan_id)
    return blocked_ids


def cmd_export(args: argparse.Namespace) -> int:
    """Export GSD plans to Superpowers format + manifest + state files."""
    input_path = args.path.resolve()
    if not input_path.exists():
        print(f"Error: {input_path} does not exist", file=sys.stderr)
        return 1

    project_root = _find_project_root(input_path)
    plan_files = find_plan_files(input_path, pending_only=args.pending)
    if not plan_files:
        print("No PLAN.md files found matching criteria.", file=sys.stderr)
        return 1

    output_dir = args.output_dir.resolve() if args.output_dir else project_root / "docs" / "plans"

    parsed_plans: list[dict[str, Any]] = []
    exported = 0
    skipped = 0
    invalid: list[tuple[str, list[str]]] = []
    warnings: list[str] = []
    rendered_plans_dir = _render_plans_dir(output_dir, project_root)

    for plan_path in plan_files:
        print(f"Processing: {plan_path.name}", file=sys.stderr)
        parsed = parse_gsd_plan(plan_path)
        plan_id = generate_plan_id(plan_path, parsed["raw_content"])

        state_dir = output_dir / "_state"
        state_path = state_dir / f"{plan_id}.json"
        existing_state = read_state(state_path)
        if existing_state and existing_state.source_plan_hash == content_hash(parsed["raw_content"]):
            skipped += 1
            parsed_plans.append(parsed)
            continue

        is_new = _is_new_plan(plan_path, project_root)
        errors = validate_plan(parsed, require_contract=is_new)
        if errors:
            invalid.append((plan_path.name, errors))
            continue

        if not parsed.get("execution_contract") and not is_new:
            warnings.append(f"  WARNING: {plan_path.name} missing <execution_contract>")

        superpowers_md = convert_to_superpowers(
            parsed,
            plan_id,
            plans_dir=rendered_plans_dir,
        )
        if args.dry_run:
            print(f"\n{'=' * 60}")
            print(f"  {plan_id}.md")
            print(f"{'=' * 60}\n")
            print(superpowers_md)
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{plan_id}.md"
            output_path.write_text(superpowers_md, encoding="utf-8")
            print(f"  -> {output_path}", file=sys.stderr)

        parsed_plans.append(parsed)
        exported += 1

    if not args.dry_run and parsed_plans:
        manifest = generate_manifest(parsed_plans, output_dir, project_root)
        manifest_path = output_dir / "_manifest.json"
        write_manifest(manifest, manifest_path)
        print(f"  -> {manifest_path}", file=sys.stderr)

    marker_path = project_root / DEPLOY_MARKER
    if not marker_path.exists() and not args.dry_run:
        marker_path.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")

    for w in warnings:
        print(w, file=sys.stderr)

    print(f"\nExported: {exported}", file=sys.stderr)
    print(f"Skipped (unchanged): {skipped}", file=sys.stderr)
    if invalid:
        print(f"Invalid: {len(invalid)}", file=sys.stderr)
        for name, errs in invalid:
            print(f"  {name}:", file=sys.stderr)
            for e in errs:
                print(f"    - {e}", file=sys.stderr)

    if not args.dry_run and exported > 0:
        next_plans_dir = str(args.output_dir) if args.output_dir else rendered_plans_dir
        print(
            f"\nNext: Run `gsd-bridge execute {next_plans_dir}`",
            file=sys.stderr,
        )
    return 1 if invalid else 0


def _sync_manifest_status(manifest_path: Path) -> Any:
    """Sync manifest statuses from state files and persist."""
    manifest = read_manifest(manifest_path)
    state_dir = manifest_path.parent / "_state"
    for entry in manifest.plans:
        state = read_state(state_dir / f"{entry.plan_id}.json")
        entry.status = state.status if state else "no-state"
    manifest.compute_summary()
    write_manifest(manifest, manifest_path)
    return manifest


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Run post-execution reconciliation and refresh manifest statuses."""
    plans_dir = args.plans_dir.resolve()
    manifest_path = plans_dir / "_manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest at {manifest_path}", file=sys.stderr)
        return 1

    project_root = _find_project_root(plans_dir)
    try:
        report = reconcile(
            manifest_path,
            project_root,
            stale_threshold_hours=float(args.stale_hours),
        )
        manifest = _sync_manifest_status(manifest_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    status_md = generate_status_md(report, manifest)
    status_path = plans_dir / "STATUS.md"
    status_path.write_text(status_md, encoding="utf-8")
    print(f"Generated: {status_path}", file=sys.stderr)

    s = report.summary
    print("\nReconciliation complete:", file=sys.stderr)
    print(f"  Total: {s.get('total', 0)}", file=sys.stderr)
    print(f"  Verified: {s.get('verified', 0)}", file=sys.stderr)
    print(f"  Pending: {s.get('pending', 0)}", file=sys.stderr)
    print(f"  Executing: {s.get('executing', 0)}", file=sys.stderr)
    print(f"  Blocked: {s.get('blocked', 0)}", file=sys.stderr)
    print(f"  Failed: {s.get('failed', 0)}", file=sys.stderr)
    print(f"  No-state: {s.get('no-state', 0)}", file=sys.stderr)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Refresh manifest statuses from state files without re-export."""
    plans_dir = args.plans_dir.resolve()
    manifest_path = plans_dir / "_manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = _sync_manifest_status(manifest_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Refreshed: {manifest_path}", file=sys.stderr)
    print(f"Plans: {manifest.summary.get('total', 0)}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Quick status check from manifest + live state files."""
    plans_dir = args.plans_dir.resolve()
    manifest_path = plans_dir / "_manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = read_manifest(manifest_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    state_dir = plans_dir / "_state"
    rows: list[dict[str, Any]] = []
    for entry in manifest.plans:
        try:
            state = read_state(state_dir / f"{entry.plan_id}.json")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if state:
            tasks_str = f"{len(state.completed_tasks)}/{state.total_tasks}"
            status = state.status
            completed = len(state.completed_tasks)
            total = state.total_tasks
        else:
            tasks_str = "?/?"
            status = "no-state"
            completed = None
            total = None
        entry.status = status
        rows.append(
            {
                "plan_id": entry.plan_id,
                "status": status,
                "tasks": {
                    "display": tasks_str,
                    "completed": completed,
                    "total": total,
                },
                "wave": entry.wave,
            }
        )

    manifest.compute_summary()
    s = manifest.summary
    if args.json:
        print(
            json.dumps(
                {
                    "manifest": str(manifest_path),
                    "plans_dir": str(plans_dir),
                    "summary": s,
                    "plans": rows,
                },
                indent=2,
            )
        )
        return 0

    print(f"{'Plan ID':<45} {'Status':<12} {'Tasks':<10} {'Wave'}")
    print(f"{'-' * 45} {'-' * 12} {'-' * 10} {'-' * 5}")
    for row in rows:
        print(
            f"{row['plan_id']:<45} "
            f"{row['status']:<12} "
            f"{row['tasks']['display']:<10} "
            f"{row['wave']}"
        )

    print(
        f"\nTotal: {s.get('total', 0)} | "
        f"Verified: {s.get('verified', 0)} | "
        f"Pending: {s.get('pending', 0)} | "
        f"Executing: {s.get('executing', 0)} | "
        f"Blocked: {s.get('blocked', 0)} | "
        f"Failed: {s.get('failed', 0)} | "
        f"No-state: {s.get('no-state', 0)}"
    )

    if getattr(args, "last_error", False):
        for row in rows:
            if row["status"] not in {"blocked", "failed"}:
                continue
            state = read_state(state_dir / f"{row['plan_id']}.json")
            if state is None:
                continue
            print(f"\n--- {row['plan_id']} ({row['status']}) ---", file=sys.stderr)
            if state.verify_quick:
                print(f"  Reproduce: {state.verify_quick}", file=sys.stderr)
            if state.last_error_output_path:
                print(f"  Last error: {state.last_error_output_path}", file=sys.stderr)
            if state.blocked_reason:
                print(f"  Reason: {state.blocked_reason}", file=sys.stderr)
            elif state.failure_reason:
                print(f"  Reason: {state.failure_reason}", file=sys.stderr)

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a GSD plan file or all pending plans."""
    if getattr(args, "pending", False):
        search_path = Path(".planning/") if args.plan_path is None else args.plan_path.resolve()
        if not search_path.exists():
            print(f"Error: {search_path} does not exist", file=sys.stderr)
            return 1
        plan_files = find_plan_files(search_path, pending_only=True)
        if not plan_files:
            print("No pending plans found.", file=sys.stderr)
            return 0

        passed = 0
        failed = 0
        for plan_path in plan_files:
            parsed = parse_gsd_plan(plan_path)
            project_root = _find_project_root(plan_path)
            is_new = _is_new_plan(plan_path, project_root)
            errs = validate_plan(parsed, require_contract=is_new)
            if errs:
                failed += 1
                print(f"FAIL: {plan_path.name}", file=sys.stderr)
                for e in errs:
                    print(f"  - {e}", file=sys.stderr)
            else:
                passed += 1
                print(f"OK:   {plan_path.name}", file=sys.stderr)

        print(f"\nValidated: {passed + failed} | Passed: {passed} | Failed: {failed}", file=sys.stderr)
        return 1 if failed else 0

    if args.plan_path is None:
        print("Error: provide a plan path or use --pending", file=sys.stderr)
        return 1

    plan_path = args.plan_path.resolve()
    if not plan_path.exists():
        print(f"Error: {plan_path} does not exist", file=sys.stderr)
        return 1

    parsed = parse_gsd_plan(plan_path)
    project_root = _find_project_root(plan_path)
    is_new = _is_new_plan(plan_path, project_root)
    errors = validate_plan(parsed, require_contract=is_new)
    if errors:
        print(f"Validation FAILED for {plan_path.name}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if getattr(args, "strict", False):
        strict_errors = _check_strict_contract(parsed)
        if strict_errors:
            print(f"Strict validation FAILED for {plan_path.name}:", file=sys.stderr)
            for e in strict_errors:
                print(f"  - {e}", file=sys.stderr)
            return 1

    print(f"Validation OK: {plan_path.name}", file=sys.stderr)
    fm = parsed["frontmatter"]
    print(f"  Phase: {fm.get('phase')}", file=sys.stderr)
    print(f"  Plan: {fm.get('plan')}", file=sys.stderr)
    print(f"  Wave: {fm.get('wave', 1)}", file=sys.stderr)
    print(f"  Tasks: {len(parsed['tasks'])}", file=sys.stderr)
    print(f"  Has execution contract: {bool(parsed.get('execution_contract'))}", file=sys.stderr)
    plan_id = generate_plan_id(plan_path, parsed["raw_content"])
    print(f"  Plan ID: {plan_id}", file=sys.stderr)
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Archive or remove a plan and its state artifacts."""
    plans_dir = args.plans_dir.resolve()
    manifest_path = plans_dir / "_manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = read_manifest(manifest_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    entry = next((p for p in manifest.plans if p.plan_id == args.plan_id), None)
    if entry is None:
        print(f"Error: plan_id not found in manifest: {args.plan_id}", file=sys.stderr)
        return 1
    if entry.status != "verified" and not args.force:
        print(
            f"Error: {args.plan_id} is status={entry.status}. Use --force to archive anyway.",
            file=sys.stderr,
        )
        return 1

    archive_dir = plans_dir / "_archive" / args.plan_id
    artifacts = {
        plans_dir / f"{args.plan_id}.md": archive_dir / f"{args.plan_id}.md",
        plans_dir / "_state" / f"{args.plan_id}.json": archive_dir / f"{args.plan_id}.json",
        plans_dir / "_blockers" / f"{args.plan_id}.md": archive_dir / f"{args.plan_id}.blocked.md",
    }
    if args.dry_run:
        print(f"Dry run: would archive {args.plan_id}", file=sys.stderr)
        for src, dst in artifacts.items():
            if src.exists():
                action = "delete" if args.delete else "move"
                print(f"  {action}: {src} -> {dst}", file=sys.stderr)

        logs_dir = plans_dir / "_logs" / args.plan_id
        if logs_dir.exists():
            action = "delete" if args.delete else "move"
            print(f"  {action}: {logs_dir}", file=sys.stderr)
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)

    for src, dst in artifacts.items():
        if not src.exists():
            continue
        if args.delete:
            src.unlink()
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    logs_dir = plans_dir / "_logs" / args.plan_id
    if logs_dir.exists():
        if args.delete:
            shutil.rmtree(logs_dir)
        else:
            shutil.move(str(logs_dir), str(archive_dir / "logs"))

    manifest.plans = [p for p in manifest.plans if p.plan_id != args.plan_id]
    manifest.compute_summary()
    write_manifest(manifest, manifest_path)
    print(f"Archived: {args.plan_id}", file=sys.stderr)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """Migrate all state files to current schema version."""
    plans_dir = args.plans_dir.resolve()
    state_dir = plans_dir / "_state"
    if not state_dir.exists():
        print(f"Error: No state directory at {state_dir}", file=sys.stderr)
        return 1

    migrated = 0
    skipped = 0
    errors = 0

    for state_file in sorted(state_dir.glob("*.json")):
        try:
            text = state_file.read_text(encoding="utf-8")
            raw = json.loads(text)
            old_version = raw.get("schema_version", "2.0")

            from .schemas import PlanState
            state = PlanState.from_json(text)

            if old_version == CURRENT_SCHEMA_VERSION:
                skipped += 1
                continue

            write_state(state_file, state)
            migrated += 1
            print(
                f"  Migrated: {state_file.name} (v{old_version} -> v{CURRENT_SCHEMA_VERSION})",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  Error migrating {state_file.name}: {exc}", file=sys.stderr)

    manifest_path = plans_dir / "_manifest.json"
    if manifest_path.exists():
        try:
            manifest = read_manifest(manifest_path)
            if manifest.version != CURRENT_SCHEMA_VERSION:
                manifest.version = CURRENT_SCHEMA_VERSION
                write_manifest(manifest, manifest_path)
                print(f"  Manifest version bumped to {CURRENT_SCHEMA_VERSION}", file=sys.stderr)
        except ValueError as exc:
            print(f"  Warning: Could not update manifest version: {exc}", file=sys.stderr)

    print(
        f"\nMigration complete: {migrated} migrated, {skipped} already current, {errors} errors",
        file=sys.stderr,
    )
    return 1 if errors else 0


def cmd_blocked(args: argparse.Namespace) -> int:
    """Show blocked plans with blocker details."""
    plans_dir = args.plans_dir.resolve()
    manifest_path = plans_dir / "_manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest at {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = read_manifest(manifest_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    state_dir = plans_dir / "_state"
    blockers_dir = plans_dir / "_blockers"
    blocked: list[dict[str, Any]] = []

    for entry in manifest.plans:
        state = read_state(state_dir / f"{entry.plan_id}.json")
        if not state or state.status != "blocked":
            continue
        blocker_content = ""
        blocker_path = blockers_dir / f"{entry.plan_id}.md"
        if blocker_path.exists():
            blocker_content = blocker_path.read_text(encoding="utf-8")
        blocked.append({
            "plan_id": entry.plan_id,
            "severity": state.blocked_severity or "high",
            "reason": state.blocked_reason or "",
            "blocker_file": blocker_content,
            "wave": entry.wave,
        })

    if not blocked:
        print("No blocked plans.", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(blocked, indent=2))
        return 0

    print(f"{'Plan ID':<45} {'Severity':<10} {'Reason'}")
    print(f"{'-' * 45} {'-' * 10} {'-' * 40}")
    for b in blocked:
        reason = b["reason"][:40] + "..." if len(b["reason"]) > 40 else b["reason"]
        print(f"{b['plan_id']:<45} {b['severity']:<10} {reason}")
    print(f"\nBlocked: {len(blocked)}", file=sys.stderr)
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    """Start eligible plan execution."""
    plans_dir = args.plans_dir.resolve()
    manifest_path = plans_dir / "_manifest.json"
    if not manifest_path.exists():
        print(f"Error: No manifest at {manifest_path}", file=sys.stderr)
        return 1

    try:
        eligible = codex_adapter.get_eligible_plans(
            manifest_path,
            wave=args.wave,
            plan_id=args.plan_id,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not eligible:
        print("No eligible plans to execute.", file=sys.stderr)
        return 0

    max_plans = args.max_plans
    if args.until_status == "blocked":
        max_plans = max(max_plans, len(eligible))

    if args.dry_run:
        print(f"{'Plan ID':<45} {'Wave':<6} {'Phase'}")
        print(f"{'-' * 45} {'-' * 6} {'-' * 20}")
        for entry in eligible[:max_plans]:
            print(f"{entry.plan_id:<45} {entry.wave:<6} {entry.phase}")
        print(f"\nWould start: {min(max_plans, len(eligible))}", file=sys.stderr)
        return 0

    started = 0
    state_dir = plans_dir / "_state"
    if args.until_status == "blocked":
        blocked_ids = _blocked_plan_ids(plans_dir)
        if blocked_ids:
            print(
                "Execution halted: blocked plan(s) present: "
                + ", ".join(sorted(blocked_ids)),
                file=sys.stderr,
            )
            return 0

    for entry in eligible[:max_plans]:
        state_path = state_dir / f"{entry.plan_id}.json"
        try:
            state = codex_adapter.start_execution(state_path.resolve())
            started += 1
            print(f"Started: {entry.plan_id} (wave {entry.wave})", file=sys.stderr)
            if args.until_status == "blocked":
                blocked_ids = _blocked_plan_ids(plans_dir)
                if blocked_ids:
                    print(
                        "Blocked: " + ", ".join(sorted(blocked_ids)),
                        file=sys.stderr,
                    )
                    break
        except (ValueError, FileNotFoundError) as exc:
            print(f"Error starting {entry.plan_id}: {exc}", file=sys.stderr)
            return 1

    print(f"\nStarted: {started}", file=sys.stderr)
    return 0


def cmd_resume_plan(args: argparse.Namespace) -> int:
    """Resume a blocked or failed plan."""
    if not args.yes:
        print("Error: resume requires --yes to confirm.", file=sys.stderr)
        return 1

    plans_dir = args.plans_dir.resolve()
    state_path = plans_dir / "_state" / f"{args.plan_id}.json"
    if not state_path.exists():
        print(f"Error: No state file at {state_path}", file=sys.stderr)
        return 1

    try:
        state = codex_adapter.resume_execution(state_path)
        print(f"Resumed: {args.plan_id} -> {state.status}", file=sys.stderr)
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_unlock(args: argparse.Namespace) -> int:
    """Force-release a stuck lease and resume execution."""
    if not args.force or not args.yes:
        print("Error: unlock requires both --force and --yes.", file=sys.stderr)
        return 1

    plans_dir = args.plans_dir.resolve()
    state_path = plans_dir / "_state" / f"{args.plan_id}.json"
    if not state_path.exists():
        print(f"Error: No state file at {state_path}", file=sys.stderr)
        return 1

    state = read_state(state_path)
    if state is None:
        print(f"Error: Could not read state for {args.plan_id}", file=sys.stderr)
        return 1

    try:
        if state.status == "executing":
            codex_adapter.mark_failed(state_path, "manual unlock: stale lease released")
            print(f"Marked failed: {args.plan_id} (lease released)", file=sys.stderr)

        result = codex_adapter.resume_execution(state_path)
        print(f"Resumed: {args.plan_id} -> {result.status}", file=sys.stderr)
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_adapter(args: argparse.Namespace) -> int:
    """Codex adapter subprocess commands."""
    try:
        if args.adapter_command == "next-plan":
            entry = codex_adapter.get_next_plan(args.manifest_path.resolve())
            print(json.dumps(entry.to_dict() if entry else None))
            return 0

        if args.adapter_command == "start":
            executor = _parse_executor(args.executor_json)
            state = codex_adapter.start_execution(args.state_path.resolve(), executor)
            print(state.to_json())
            return 0

        if args.adapter_command == "resume":
            executor = _parse_executor(args.executor_json)
            state = codex_adapter.resume_execution(args.state_path.resolve(), executor)
            print(state.to_json())
            return 0

        if args.adapter_command == "complete-task":
            state = codex_adapter.complete_task(args.state_path.resolve(), args.task_num)
            print(state.to_json())
            return 0

        if args.adapter_command == "record-verification":
            state = codex_adapter.record_verification(
                args.state_path.resolve(),
                args.tier,
                args.verify_command,
                args.exit_code,
                args.log_path,
            )
            print(state.to_json())
            return 0

        if args.adapter_command == "mark-verified":
            state = codex_adapter.mark_verified(args.state_path.resolve())
            print(state.to_json())
            return 0

        if args.adapter_command == "mark-failed":
            state = codex_adapter.mark_failed(args.state_path.resolve(), args.reason)
            print(state.to_json())
            return 0

        if args.adapter_command == "renew-lock":
            state = codex_adapter.renew_lock(args.state_path.resolve(), args.run_id)
            print(state.to_json())
            return 0

        if args.adapter_command == "advance-step":
            state = codex_adapter.advance_step(args.state_path.resolve(), args.step)
            print(state.to_json())
            return 0

        if args.adapter_command == "mark-blocked":
            state = codex_adapter.mark_blocked(
                args.state_path.resolve(),
                args.reason,
                args.missing_info,
                args.unblock_command,
                who_must_answer=args.who_must_answer,
                severity=args.severity,
                resume_command=args.resume_command,
            )
            print(state.to_json())
            return 0

        if args.adapter_command == "rollback":
            result = codex_adapter.rollback_execution(
                args.manifest_path.resolve(),
                args.state_path.resolve(),
                args.rollback_command,
            )
            print(json.dumps(result))
            return 0

        print(f"Unknown adapter command: {args.adapter_command}", file=sys.stderr)
        return 1
    except GSDError as exc:
        print(f"Adapter error [{exc.category}]: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"Adapter error [input]: {exc}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(f"Adapter error [integration]: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Adapter error [logic]: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Adapter error: {exc}", file=sys.stderr)
        return 1


def _parse_executor(executor_json: str) -> dict[str, Any] | None:
    if not executor_json:
        return None
    parsed = json.loads(executor_json)
    if not isinstance(parsed, dict):
        raise ValueError("--executor-json must parse to an object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gsd-bridge",
        description="GSD Bridge v3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              gsd-bridge export --pending
              gsd-bridge status
              gsd-bridge blocked
              gsd-bridge execute --dry-run
              gsd-bridge execute --wave 14 --until blocked --max-plans 100
              gsd-bridge validate --pending
              gsd-bridge resume PLAN_ID --yes
              gsd-bridge unlock PLAN_ID --force --yes
              gsd-bridge reconcile
              gsd-bridge adapter next-plan docs/plans/_manifest.json
            """
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export plans to manifest/state")
    export_parser.add_argument("path", type=Path, nargs="?", default=Path(".planning/"))
    export_parser.add_argument("-o", "--output-dir", type=Path, default=None)
    export_parser.add_argument("--pending", action="store_true")
    export_parser.add_argument("--dry-run", action="store_true")

    reconcile_parser = subparsers.add_parser("reconcile", help="Reconcile + STATUS.md")
    reconcile_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    reconcile_parser.add_argument(
        "--stale-hours",
        type=float,
        default=STALE_THRESHOLD_HOURS,
        help=f"Stale execution threshold in hours (default: {STALE_THRESHOLD_HOURS})",
    )

    status_parser = subparsers.add_parser("status", help="Quick status table")
    status_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status_parser.add_argument(
        "--last-error", action="store_true",
        help="Show last error + reproduce command for blocked/failed plans",
    )

    refresh_parser = subparsers.add_parser("refresh", help="Sync manifest status from state files")
    refresh_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))

    validate_parser = subparsers.add_parser("validate", help="Validate a GSD plan file")
    validate_parser.add_argument("plan_path", type=Path, nargs="?", default=None)
    validate_parser.add_argument("--pending", action="store_true", help="Validate all pending plans")
    validate_parser.add_argument(
        "--strict", action="store_true",
        help="Also check contract completeness (inputs, outputs, side_effects, rollback)",
    )

    migrate_parser = subparsers.add_parser("migrate", help="Migrate state files to current schema")
    migrate_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))

    archive_parser = subparsers.add_parser("archive", help="Archive/remove plan artifacts")
    archive_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    archive_parser.add_argument("plan_id")
    archive_parser.add_argument("--force", action="store_true")
    archive_parser.add_argument("--delete", action="store_true")
    archive_parser.add_argument("--dry-run", action="store_true")

    blocked_parser = subparsers.add_parser("blocked", help="Show blocked plans")
    blocked_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    blocked_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    execute_parser = subparsers.add_parser("execute", help="Start eligible plan execution")
    execute_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    execute_parser.add_argument("--plan", dest="plan_id", default=None, help="Target specific plan ID")
    execute_parser.add_argument("--wave", type=int, default=None, help="Filter by wave number")
    execute_parser.add_argument("--max-plans", type=int, default=1, help="Max plans to start")
    execute_parser.add_argument("--until", dest="until_status", choices=["blocked"], default=None)
    execute_parser.add_argument("--dry-run", action="store_true", help="Preview without starting")

    resume_parser = subparsers.add_parser("resume", help="Resume blocked/failed plan")
    resume_parser.add_argument("plan_id")
    resume_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    resume_parser.add_argument("--yes", action="store_true", help="Skip confirmation")

    unlock_parser = subparsers.add_parser("unlock", help="Force-release stuck lease")
    unlock_parser.add_argument("plan_id")
    unlock_parser.add_argument("plans_dir", type=Path, nargs="?", default=Path("docs/plans"))
    unlock_parser.add_argument("--force", action="store_true")
    unlock_parser.add_argument("--yes", action="store_true")

    adapter_parser = subparsers.add_parser("adapter", help="Codex adapter subprocess API")
    adapter_subparsers = adapter_parser.add_subparsers(dest="adapter_command", required=True)

    adapter_next = adapter_subparsers.add_parser("next-plan")
    adapter_next.add_argument("manifest_path", type=Path)

    adapter_start = adapter_subparsers.add_parser("start")
    adapter_start.add_argument("state_path", type=Path)
    adapter_start.add_argument("--executor-json", default="")

    adapter_resume = adapter_subparsers.add_parser("resume")
    adapter_resume.add_argument("state_path", type=Path)
    adapter_resume.add_argument("--executor-json", default="")

    adapter_complete = adapter_subparsers.add_parser("complete-task")
    adapter_complete.add_argument("state_path", type=Path)
    adapter_complete.add_argument("task_num", type=int)

    adapter_verify = adapter_subparsers.add_parser("record-verification")
    adapter_verify.add_argument("state_path", type=Path)
    adapter_verify.add_argument("tier")
    adapter_verify.add_argument("verify_command")
    adapter_verify.add_argument("exit_code", type=int)
    adapter_verify.add_argument("--log-path", default="")

    adapter_verified = adapter_subparsers.add_parser("mark-verified")
    adapter_verified.add_argument("state_path", type=Path)

    adapter_failed = adapter_subparsers.add_parser("mark-failed")
    adapter_failed.add_argument("state_path", type=Path)
    adapter_failed.add_argument("--reason", default="")

    adapter_blocked = adapter_subparsers.add_parser("mark-blocked")
    adapter_blocked.add_argument("state_path", type=Path)
    adapter_blocked.add_argument("reason")
    adapter_blocked.add_argument("--missing-info", default="")
    adapter_blocked.add_argument("--unblock-command", default="")
    adapter_blocked.add_argument("--who-must-answer", default="")
    adapter_blocked.add_argument(
        "--severity", default="high",
        choices=["critical", "high", "medium", "low"],
    )
    adapter_blocked.add_argument("--resume-command", default="")

    adapter_renew = adapter_subparsers.add_parser("renew-lock")
    adapter_renew.add_argument("state_path", type=Path)
    adapter_renew.add_argument("run_id")

    adapter_step = adapter_subparsers.add_parser("advance-step")
    adapter_step.add_argument("state_path", type=Path)
    adapter_step.add_argument("step", type=int)

    adapter_rollback = adapter_subparsers.add_parser("rollback")
    adapter_rollback.add_argument("manifest_path", type=Path)
    adapter_rollback.add_argument("state_path", type=Path)
    adapter_rollback.add_argument("--rollback-command", default="")

    args = parser.parse_args(argv)

    # Initialize structured logging for this invocation
    run_id = new_run_id()
    log_command = args.command
    if args.command == "adapter" and hasattr(args, "adapter_command"):
        log_command = f"adapter.{args.adapter_command}"
    configure_logging(run_id=run_id, command=log_command)
    _log = get_logger("cli")
    _log.debug("invocation: %s", log_command)

    commands = {
        "export": cmd_export,
        "reconcile": cmd_reconcile,
        "status": cmd_status,
        "refresh": cmd_refresh,
        "validate": cmd_validate,
        "migrate": cmd_migrate,
        "archive": cmd_archive,
        "blocked": cmd_blocked,
        "execute": cmd_execute,
        "resume": cmd_resume_plan,
        "unlock": cmd_unlock,
        "adapter": cmd_adapter,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

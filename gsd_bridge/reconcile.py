"""Post-execution reconciliation.

Run after Codex finishes executing plans to:
  1. Detect drift (source plan changed since export)
  2. Flag missing verification (tasks done but no verification result)
  3. Flag stale execution (executing for > 24h with no progress)
  4. Generate STATUS.md human-readable dashboard
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .manifest import read_manifest
from .plan_id import content_hash
from .schemas import (
    DriftWarning,
    Manifest,
    ReconcileIssue,
    ReconcileReport,
    _now_iso,
)
from .state import read_state

STALE_THRESHOLD_HOURS = 24


def reconcile(
    manifest_path: Path,
    project_root: Path,
    stale_threshold_hours: float = STALE_THRESHOLD_HOURS,
) -> ReconcileReport:
    """Run full reconciliation and return a report."""
    manifest = read_manifest(manifest_path)
    plans_dir = manifest_path.parent
    state_dir = plans_dir / "_state"

    report = ReconcileReport()
    status_counts: dict[str, int] = {"total": 0, "no-state": 0}

    for entry in manifest.plans:
        status_counts["total"] += 1
        state_path = state_dir / f"{entry.plan_id}.json"
        state = read_state(state_path)

        if state is None:
            status_counts["no-state"] += 1
            report.issues.append(ReconcileIssue(
                plan_id=entry.plan_id,
                issue_type="missing_state",
                description=f"No state file found at {state_path}",
            ))
            continue

        report.plan_states.append(state)
        status_counts[state.status] = status_counts.get(state.status, 0) + 1

        # Check for drift
        source_path = project_root / entry.source_path
        if source_path.exists():
            current_hash = content_hash(
                source_path.read_text(encoding="utf-8")
            )
            if current_hash != state.source_plan_hash:
                report.drift_warnings.append(DriftWarning(
                    plan_id=entry.plan_id,
                    source_path=str(source_path),
                    expected_hash=state.source_plan_hash,
                    actual_hash=current_hash,
                ))
                report.issues.append(ReconcileIssue(
                    plan_id=entry.plan_id,
                    issue_type="drift",
                    description=(
                        f"Source plan changed since export. "
                        f"Expected hash {state.source_plan_hash[:12]}..., "
                        f"got {current_hash[:12]}..."
                    ),
                ))

        # Check for missing verification on completed tasks
        if state.completed_tasks and not state.verification:
            report.issues.append(ReconcileIssue(
                plan_id=entry.plan_id,
                issue_type="verification_missing",
                description=(
                    f"Tasks {state.completed_tasks} completed but no "
                    f"verification results recorded"
                ),
            ))

        # Check for stale execution
        if state.status == "executing" and state.last_run_at:
            try:
                last_run = datetime.fromisoformat(state.last_run_at)
                age_hours = (
                    datetime.now(timezone.utc) - last_run
                ).total_seconds() / 3600
                if age_hours > stale_threshold_hours:
                    report.issues.append(ReconcileIssue(
                        plan_id=entry.plan_id,
                        issue_type="stale_execution",
                        description=(
                            f"Executing for {age_hours:.1f}h with no progress "
                            f"(threshold: {stale_threshold_hours}h)"
                        ),
                    ))
            except (ValueError, TypeError):
                pass

        # Check for expired locks
        if state.status == "executing" and state.lock:
            try:
                expires_at = datetime.fromisoformat(
                    state.lock.get("expires_at", "")
                )
                if datetime.now(timezone.utc) > expires_at:
                    report.issues.append(ReconcileIssue(
                        plan_id=entry.plan_id,
                        issue_type="expired_lock",
                        description=(
                            f"Lock held by run_id={state.lock.get('run_id', '?')} "
                            f"expired at {state.lock.get('expires_at', '?')}. "
                            f"Next executor will perform recovery takeover."
                        ),
                    ))
            except (ValueError, TypeError):
                pass

        # Report recovery events
        if state.recovery_notes:
            for note in state.recovery_notes:
                report.issues.append(ReconcileIssue(
                    plan_id=entry.plan_id,
                    issue_type="lock_recovery",
                    description=(
                        f"Lease takeover at {note.get('taken_over_at', '?')}: "
                        f"previous run_id={note.get('previous_run_id', '?')}"
                    ),
                ))

    report.summary = status_counts
    return report


def generate_status_md(report: ReconcileReport, manifest: Manifest) -> str:
    """Generate human-readable STATUS.md dashboard."""
    lines: list[str] = []

    lines.append("# Bridge Execution Status")
    lines.append("")
    lines.append(f"> Generated: {_now_iso()}")
    lines.append("")

    # Summary
    s = report.summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Total plans | {s.get('total', 0)} |")
    lines.append(f"| Verified | {s.get('verified', 0)} |")
    lines.append(f"| Pending | {s.get('pending', 0)} |")
    lines.append(f"| Executing | {s.get('executing', 0)} |")
    lines.append(f"| Blocked | {s.get('blocked', 0)} |")
    lines.append(f"| Failed | {s.get('failed', 0)} |")
    lines.append("")

    # Per-plan table
    lines.append("## Plan Status")
    lines.append("")
    lines.append("| Plan ID | Status | Tasks | Last Run | Issues |")
    lines.append("|---------|--------|-------|----------|--------|")

    plan_issues: dict[str, list[str]] = {}
    for issue in report.issues:
        plan_issues.setdefault(issue.plan_id, []).append(issue.issue_type)

    for state in report.plan_states:
        tasks_str = f"{len(state.completed_tasks)}/{state.total_tasks}"
        last_run = state.last_run_at[:10] if state.last_run_at else "—"
        issues = ", ".join(plan_issues.get(state.plan_id, [])) or "—"
        status_icon = _status_icon(state.status)
        lines.append(
            f"| `{state.plan_id}` | {status_icon} {state.status} | "
            f"{tasks_str} | {last_run} | {issues} |"
        )
    lines.append("")

    # Per-plan verification tier detail
    states_with_verification = [s for s in report.plan_states if s.verification]
    if states_with_verification:
        lines.append("## Verification Detail")
        lines.append("")
        lines.append("| Plan ID | quick | full | smoke |")
        lines.append("|---------|-------|------|-------|")
        for state in states_with_verification:
            v = state.verification or {}
            quick_status = _tier_status_icon(v.get("quick"))
            full_status = _tier_status_icon(v.get("full"))
            smoke_status = _tier_status_icon(v.get("smoke"))
            lines.append(
                f"| `{state.plan_id}` | {quick_status} | {full_status} | {smoke_status} |"
            )
        lines.append("")

    # Drift warnings
    if report.drift_warnings:
        lines.append("## Drift Warnings")
        lines.append("")
        lines.append("These plans have changed since export. Re-run `gsd-bridge export` to update.")
        lines.append("")
        for dw in report.drift_warnings:
            lines.append(f"- **{dw.plan_id}**: `{dw.source_path}`")
            lines.append(f"  - Expected: `{dw.expected_hash[:12]}...`")
            lines.append(f"  - Actual: `{dw.actual_hash[:12]}...`")
        lines.append("")

    # Blockers
    blocked_states = [s for s in report.plan_states if s.status == "blocked"]
    if blocked_states:
        lines.append("## Blocked Plans")
        lines.append("")
        for bs in blocked_states:
            reason = bs.blocked_reason or "No reason recorded"
            severity = bs.blocked_severity or "high"
            severity_icon = {
                "critical": "!!!!",
                "high": "!!!",
                "medium": "!!",
                "low": "!",
            }.get(severity, "!!")
            lines.append(f"- [{severity_icon}] **{bs.plan_id}** ({severity.upper()}): {reason}")
            lines.append(f"  - Blocker file: `docs/plans/_blockers/{bs.plan_id}.md`")
        lines.append("")

    # All issues
    if report.issues:
        lines.append("## All Issues")
        lines.append("")
        for issue in report.issues:
            lines.append(f"- [{issue.issue_type}] **{issue.plan_id}**: {issue.description}")
        lines.append("")

    return "\n".join(lines)


def _tier_status_icon(results: list[dict[str, object]] | None) -> str:
    """Return a status icon for a verification tier's results list."""
    if results is None or len(results) == 0:
        return "---"
    last = results[-1]
    if last.get("exit_code", -1) == 0:
        return "PASS"
    return "FAIL"


def _status_icon(status: str) -> str:
    icons = {
        "verified": "[OK]",
        "pending": "[..]",
        "executing": "[>>]",
        "blocked": "[!!]",
        "failed": "[XX]",
    }
    return icons.get(status, "[??]")

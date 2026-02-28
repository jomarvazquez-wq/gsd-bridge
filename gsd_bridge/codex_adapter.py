"""Integration helpers for Codex subprocess or direct Python usage.

Usage from Codex (Python):
    from gsd_bridge.codex_adapter import get_next_plan, start_execution, ...

Usage from Codex (subprocess):
    python -m gsd_bridge adapter next-plan docs/plans/_manifest.json
    python -m gsd_bridge adapter start docs/plans/_state/<plan_id>.json
    python -m gsd_bridge adapter resume docs/plans/_state/<plan_id>.json
    python -m gsd_bridge adapter rollback docs/plans/_manifest.json docs/plans/_state/<plan_id>.json
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .log import get_logger
from .manifest import read_manifest
from .plan_id import content_hash
from .schemas import Manifest, ManifestEntry, PlanState, _now_iso
from .state import (
    acquire_lease,
    advance_step as _advance_step,
    apply_state_patch,
    complete_task as _complete_task,
    read_state,
    record_verification as _record_verification,
    release_lease,
    renew_lease as _renew_lease,
    state_lock,
    transition,
    write_state,
)

_log = get_logger("adapter")


DEFAULT_ROLLBACK_ALLOWLIST = {
    "echo",
    "git",
    "make",
    "npm",
    "npx",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "uv",
    "yarn",
}


def _ensure_executing(state: PlanState, action: str) -> None:
    if state.status != "executing":
        raise ValueError(f"{action} requires executing state, got: {state.status}")


def get_next_plan(manifest_path: Path) -> ManifestEntry | None:
    """Return next executable plan respecting wave and depends_on ordering."""
    eligible = get_eligible_plans(manifest_path)
    if eligible:
        _log.debug("next_plan: %s (wave=%d)", eligible[0].plan_id, eligible[0].wave)
    else:
        _log.debug("next_plan: none eligible")
    return eligible[0] if eligible else None


def get_eligible_plans(
    manifest_path: Path,
    *,
    wave: int | None = None,
    plan_id: str | None = None,
) -> list[ManifestEntry]:
    """Return all eligible plans (pending + dependencies met), optionally filtered."""
    manifest = read_manifest(manifest_path)
    state_dir = manifest_path.parent / "_state"
    statuses = _current_status_by_plan(manifest, state_dir)

    eligible: list[ManifestEntry] = []
    for entry in manifest.plans:
        if statuses.get(entry.plan_id) != "pending":
            continue
        if plan_id and entry.plan_id != plan_id:
            continue
        if wave is not None and entry.wave != wave:
            continue
        if not _lower_waves_verified(entry, manifest, statuses):
            continue
        if not _dependencies_verified(entry, manifest, statuses):
            continue
        eligible.append(entry)
    return eligible


def start_execution(
    state_path: Path,
    executor_info: dict[str, Any] | None = None,
) -> PlanState:
    """Transition plan to executing under a state-file lock + logical lease."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        if state.status != "pending":
            raise ValueError(f"Start requires pending state, got: {state.status}")
        _ensure_no_source_drift(state_path=state_path, state=state)
        run_id = (executor_info or {}).get("run_id") or str(uuid.uuid4())
        _log.debug("start: %s run_id=%s", state.plan_id, run_id[:8])
        state = acquire_lease(state, run_id=run_id)
        state.cursor = {"task": 1, "step": 0}
        state.git_sha_before = _capture_git_sha()
        state = transition(
            state,
            "executing",
            executor=executor_info,
            blocked_reason=None,
        )
        write_state(state_path, state)
        return state


def resume_execution(
    state_path: Path,
    executor_info: dict[str, Any] | None = None,
) -> PlanState:
    """Resume a blocked/failed plan back to executing. Acquires a new lease."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        if state.status not in {"blocked", "failed"}:
            raise ValueError(
                f"Resume requires blocked/failed state, got: {state.status}"
            )
        _ensure_no_source_drift(state_path=state_path, state=state)
        run_id = (executor_info or {}).get("run_id") or str(uuid.uuid4())
        _log.debug("resume: %s run_id=%s", state.plan_id, run_id[:8])
        if state.lock and not _lease_is_expired(state.lock):
            current_run_id = state.lock.get("run_id")
            if current_run_id and current_run_id != run_id:
                state.recovery_notes.append(
                    {
                        "event": "lease_handoff",
                        "previous_run_id": current_run_id,
                        "taken_over_at": _now_iso(),
                        "new_run_id": run_id,
                    }
                )
                state.lock = None
        state = acquire_lease(state, run_id=run_id)
        state = transition(
            state,
            "executing",
            executor=executor_info,
            blocked_reason=None,
        )
        write_state(state_path, state)
        return state


def complete_task(state_path: Path, task_num: int) -> PlanState:
    """Mark a task as completed in the state file."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        _ensure_executing(state, "complete-task")
        state = _complete_task(state, task_num)
        write_state(state_path, state)
        return state


def advance_step(state_path: Path, step: int) -> PlanState:
    """Advance the cursor step within the current task."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        _ensure_executing(state, "advance-step")
        state = _advance_step(state, step)
        write_state(state_path, state)
        return state


def renew_lock(state_path: Path, run_id: str) -> PlanState:
    """Renew the logical lease. Executor should call this periodically."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        state = _renew_lease(state, run_id)
        write_state(state_path, state)
        return state


def record_verification(
    state_path: Path,
    tier: str,
    command: str,
    exit_code: int,
    log_path: str = "",
) -> PlanState:
    """Record one verification event and ensure log file persistence."""
    _log.debug("verify: tier=%s exit=%d cmd=%s", tier, exit_code, command[:60])
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        _ensure_executing(state, "record-verification")

        plans_dir, project_root = _resolve_runtime_paths(state_path=state_path)
        resolved_log_path = _ensure_log_file(
            plans_dir=plans_dir,
            project_root=project_root,
            plan_id=state.plan_id,
            tier=tier,
            command=command,
            exit_code=exit_code,
            requested_log_path=log_path,
        )

        # Store first passing quick-tier command as the reproduce command
        if tier == "quick" and exit_code == 0 and not state.verify_quick:
            state.verify_quick = command

        # Track last failed verification output for --last-error triage
        if exit_code != 0:
            state.last_error_output_path = resolved_log_path
            _ensure_debug_fixture(
                plans_dir=plans_dir,
                plan_id=state.plan_id,
                tier=tier,
                command=command,
                log_path=resolved_log_path,
            )

        state = _record_verification(
            state,
            tier=tier,
            command=command,
            exit_code=exit_code,
            log_path=resolved_log_path,
        )
        write_state(state_path, state)
        return state


def mark_verified(state_path: Path) -> PlanState:
    """Transition executing → verified. Releases lease."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        _ensure_executing(state, "mark-verified")
        if len(set(state.completed_tasks)) < state.total_tasks:
            raise ValueError("Cannot mark verified: not all tasks are completed")
        verification_results = state.verification or {}
        has_results = any(
            isinstance(results, list) and len(results) > 0
            for results in verification_results.values()
        )
        if not has_results:
            raise ValueError("Cannot mark verified: verification results are required")
        state.git_sha_after = _capture_git_sha()
        state = transition(state, "verified")
        state = release_lease(state)
        _log.debug("verified: %s", state.plan_id)
        write_state(state_path, state)
        return state


def mark_failed(state_path: Path, reason: str = "") -> PlanState:
    """Transition executing → failed. Releases lease."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        state = transition(
            state,
            "failed",
            failure_reason=reason,
            blocked_reason=None,
        )
        state = release_lease(state)
        write_state(state_path, state)
        return state


def mark_blocked(
    state_path: Path,
    reason: str,
    missing_info: str = "",
    unblock_command: str = "",
    who_must_answer: str = "",
    severity: str = "high",
    resume_command: str = "",
) -> PlanState:
    """Transition executing → blocked and write blocker markdown."""
    _log.debug("blocked: severity=%s reason=%s", severity, reason[:80])
    from .schemas import VALID_SEVERITIES

    if severity not in VALID_SEVERITIES:
        severity = "high"

    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        state = transition(
            state,
            "blocked",
            blocked_reason=reason,
            blocked_severity=severity,
            failure_reason=None,
        )
        write_state(state_path, state)

    plans_dir, _project_root = _resolve_runtime_paths(state_path=state_path)
    blockers_dir = plans_dir / "_blockers"
    blockers_dir.mkdir(parents=True, exist_ok=True)
    blocker_path = blockers_dir / f"{state.plan_id}.md"

    blocker_content = [
        f"# Blocked: {state.plan_id}",
        "",
        f"> Blocked at: {_now_iso()}",
        f"> Severity: **{severity.upper()}**",
        "",
    ]
    if who_must_answer:
        blocker_content.extend(
            [
                "## Who must answer",
                "",
                who_must_answer,
                "",
            ]
        )
    blocker_content.extend(
        [
            "## What's blocked",
            "",
            reason,
            "",
        ]
    )
    if missing_info:
        blocker_content.extend(
            [
                "## Missing information",
                "",
                missing_info,
                "",
            ]
        )
    if resume_command:
        blocker_content.extend(
            [
                "## Resume command",
                "",
                "```bash",
                resume_command,
                "```",
                "",
            ]
        )
    elif unblock_command:
        blocker_content.extend(
            [
                "## To unblock",
                "",
                "```bash",
                unblock_command,
                "```",
                "",
            ]
        )

    blocker_path.write_text("\n".join(blocker_content), encoding="utf-8")
    return state


def rollback_execution(
    manifest_path: Path,
    state_path: Path,
    command: str = "",
) -> dict[str, Any]:
    """Execute rollback from structured argv contract + allowlist; capture log."""
    state = read_state(state_path)
    if state is None:
        raise FileNotFoundError(f"State file not found: {state_path}")

    manifest = read_manifest(manifest_path)
    entry = next((p for p in manifest.plans if p.plan_id == state.plan_id), None)
    if entry is None:
        raise ValueError(f"Plan {state.plan_id} not found in manifest")

    plans_dir, project_root = _resolve_runtime_paths(
        state_path=state_path,
        manifest_path=manifest_path,
        project_root_hint=manifest.project_root,
    )
    rollback_spec = _resolve_rollback_spec(
        command=command,
        execution_contract=entry.execution_contract or {},
    )
    argv, cwd, env = _normalize_rollback_spec(rollback_spec, project_root)
    _enforce_rollback_allowlist(argv[0])
    _log.debug("rollback: %s argv=%s", state.plan_id, argv)

    result = subprocess.run(
        argv,
        shell=False,
        text=True,
        capture_output=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    command_text = shlex.join(argv)
    log_path = _write_rollback_log(
        plans_dir=plans_dir,
        project_root=project_root,
        plan_id=state.plan_id,
        command=command_text,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )

    # Persist latest rollback outcome on state for auditability.
    state.last_run_at = _now_iso()
    if result.returncode != 0:
        state.failure_reason = (
            state.failure_reason
            or f"Rollback failed with exit code {result.returncode}"
        )
    write_state(state_path, state)

    return {
        "plan_id": state.plan_id,
        "argv": argv,
        "command": command_text,
        "exit_code": result.returncode,
        "log_path": log_path,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def check_drift(state_path: Path, source_plan_path: Path) -> bool:
    """Return True if source plan hash differs from state's recorded hash."""
    state = read_state(state_path)
    if state is None:
        return False
    if not source_plan_path.exists():
        return True

    current_hash = content_hash(source_plan_path.read_text(encoding="utf-8"))
    return current_hash != state.source_plan_hash


def _ensure_no_source_drift(*, state_path: Path, state: PlanState) -> None:
    _plans_dir, project_root = _resolve_runtime_paths(state_path=state_path)
    source_path = Path(state.source_plan_path)
    if not source_path.is_absolute():
        source_path = (project_root / source_path).resolve()
    if source_path.exists() and check_drift(state_path, source_path):
        raise ValueError(
            f"Cannot continue: source plan drift detected for {state.plan_id}. "
            f"Re-export plans before execution."
        )


def update_state(state_path: Path, **kwargs: Any) -> PlanState:
    """Apply a validated patch to a state file."""
    with state_lock(state_path):
        state = read_state(state_path)
        if state is None:
            raise FileNotFoundError(f"State file not found: {state_path}")
        state = apply_state_patch(state, kwargs)
        write_state(state_path, state)
        return state


def _current_status_by_plan(
    manifest: Manifest,
    state_dir: Path,
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for entry in manifest.plans:
        state = read_state(state_dir / f"{entry.plan_id}.json")
        if state is None:
            statuses[entry.plan_id] = entry.status or "no-state"
        else:
            statuses[entry.plan_id] = state.status
    return statuses


def _lower_waves_verified(
    entry: ManifestEntry,
    manifest: Manifest,
    statuses: dict[str, str],
) -> bool:
    for other in manifest.plans:
        if other.wave < entry.wave and statuses.get(other.plan_id) != "verified":
            return False
    return True


def _dependencies_verified(
    entry: ManifestEntry,
    manifest: Manifest,
    statuses: dict[str, str],
) -> bool:
    if not entry.depends_on:
        return True

    lookup: dict[str, set[str]] = {}
    for other in manifest.plans:
        tokens = {
            other.plan_id,
            str(other.plan_number),
            f"{other.phase}:{other.plan_number}",
            f"{other.phase}-{other.plan_number:02d}",
            other.source_path,
            Path(other.source_path).name,
            Path(other.source_path).stem,
        }
        for token in tokens:
            normalized = str(token).strip().lower()
            if not normalized:
                continue
            lookup.setdefault(normalized, set()).add(other.plan_id)

    for dep in entry.depends_on:
        token = str(dep).strip().lower()
        matched_plan_ids = lookup.get(token)
        if not matched_plan_ids:
            return False
        if len(matched_plan_ids) > 1:
            raise ValueError(
                f"Ambiguous dependency '{dep}' for {entry.plan_id}: "
                f"matches {sorted(matched_plan_ids)}"
            )
        matched_plan_id = next(iter(matched_plan_ids))
        if statuses.get(matched_plan_id) != "verified":
            return False
    return True


def _resolve_runtime_paths(
    *,
    state_path: Path,
    manifest_path: Path | None = None,
    project_root_hint: str = "",
) -> tuple[Path, Path]:
    plans_override = os.getenv("GSD_BRIDGE_PLANS_DIR", "").strip()
    project_root_override = os.getenv("GSD_BRIDGE_PROJECT_ROOT", "").strip()

    if plans_override:
        plans_dir = Path(plans_override).expanduser().resolve()
    elif manifest_path is not None:
        plans_dir = manifest_path.resolve().parent
    else:
        plans_dir = _infer_plans_dir_from_state_path(state_path)

    if project_root_override:
        project_root = Path(project_root_override).expanduser().resolve()
    elif project_root_hint:
        project_root = Path(project_root_hint).expanduser().resolve()
    else:
        project_root = _infer_project_root_from_plans_dir(plans_dir)

    return plans_dir, project_root


def _infer_plans_dir_from_state_path(state_path: Path) -> Path:
    resolved = state_path.resolve()
    for parent in [resolved.parent] + list(resolved.parents):
        if parent.name == "_state":
            return parent.parent
    return resolved.parent


def _infer_project_root_from_plans_dir(plans_dir: Path) -> Path:
    resolved = plans_dir.resolve()
    for parent in [resolved] + list(resolved.parents):
        if (parent / ".planning").is_dir() or (parent / ".git").exists():
            return parent
    parents = list(resolved.parents)
    if len(parents) >= 2:
        return parents[1]
    if parents:
        return parents[0]
    return resolved


def _resolve_rollback_spec(command: str, execution_contract: dict[str, Any]) -> Any:
    override = command.strip()
    if override:
        try:
            return json.loads(override)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "--rollback-command must be JSON (array argv or object with argv)"
            ) from exc
    return execution_contract.get("rollback")


def _normalize_rollback_spec(
    rollback_spec: Any,
    project_root: Path,
) -> tuple[list[str], Path | None, dict[str, str] | None]:
    if rollback_spec is None:
        raise ValueError("No rollback command available for plan")
    if isinstance(rollback_spec, str):
        raise ValueError(
            "Rollback must be structured JSON with argv (raw shell strings are not allowed)"
        )

    cwd: Path | None = None
    env: dict[str, str] | None = None

    if isinstance(rollback_spec, list):
        argv = _validate_argv(rollback_spec)
    elif isinstance(rollback_spec, dict):
        argv = _validate_argv(rollback_spec.get("argv"))
        cwd = _validate_cwd(rollback_spec.get("cwd"), project_root)
        env = _validate_env(rollback_spec.get("env"))
    else:
        raise ValueError("Rollback spec must be an argv list or object with argv")

    return argv, cwd, env


def _validate_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("Rollback argv must be a non-empty array")
    argv: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Rollback argv values must be non-empty strings")
        argv.append(item)
    return argv


def _validate_cwd(value: Any, project_root: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Rollback cwd must be a non-empty string")
    cwd = Path(value)
    if not cwd.is_absolute():
        cwd = (project_root / cwd).resolve()
    else:
        cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ValueError(f"Rollback cwd does not exist: {cwd}")
    return cwd


def _validate_env(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Rollback env must be an object")

    merged = os.environ.copy()
    for key, env_value in value.items():
        if not isinstance(key, str):
            raise ValueError("Rollback env keys must be strings")
        if not isinstance(env_value, str):
            raise ValueError("Rollback env values must be strings")
        merged[key] = env_value
    return merged


def _lease_is_expired(lock: dict[str, Any]) -> bool:
    expires_at = lock.get("expires_at")
    if not isinstance(expires_at, str):
        return True
    try:
        return datetime.fromisoformat(expires_at).timestamp() <= datetime.now(
            timezone.utc
        ).timestamp()
    except ValueError:
        return True


def _rollback_allowlist() -> set[str]:
    raw = os.getenv("GSD_BRIDGE_ROLLBACK_ALLOWLIST", "").strip()
    if raw:
        return {token.strip() for token in raw.split(",") if token.strip()}
    return set(DEFAULT_ROLLBACK_ALLOWLIST)


def _enforce_rollback_allowlist(command: str) -> None:
    executable = Path(command).name
    allowlist = _rollback_allowlist()
    if executable not in allowlist:
        raise ValueError(
            f"Rollback command '{executable}' is not in allowlist: {sorted(allowlist)}"
        )


def _ensure_log_file(
    *,
    plans_dir: Path,
    project_root: Path,
    plan_id: str,
    tier: str,
    command: str,
    exit_code: int,
    requested_log_path: str,
) -> str:
    if requested_log_path:
        resolved = Path(requested_log_path)
        if not resolved.is_absolute():
            resolved = project_root / resolved
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        resolved = plans_dir / "_logs" / plan_id / f"{tier}-{stamp}.log"

    resolved.parent.mkdir(parents=True, exist_ok=True)
    if not resolved.exists():
        resolved.write_text(
            "\n".join(
                [
                    f"# Verification ({tier})",
                    f"Recorded at: {_now_iso()}",
                    f"Command: {command}",
                    f"Exit code: {exit_code}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    try:
        return str(resolved.relative_to(project_root))
    except ValueError:
        return str(resolved)


def _write_rollback_log(
    *,
    plans_dir: Path,
    project_root: Path,
    plan_id: str,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_path = plans_dir / "_logs" / plan_id / f"rollback-{stamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "# Rollback Execution",
                f"Recorded at: {_now_iso()}",
                f"Command: {command}",
                f"Exit code: {exit_code}",
                "",
                "## STDOUT",
                stdout.rstrip(),
                "",
                "## STDERR",
                stderr.rstrip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        return str(log_path.relative_to(project_root))
    except ValueError:
        return str(log_path)


def _capture_git_sha() -> str | None:
    """Return current HEAD SHA, or None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _ensure_debug_fixture(
    *,
    plans_dir: Path,
    plan_id: str,
    tier: str,
    command: str,
    log_path: str,
) -> str:
    """Auto-save a debug fixture when verification fails.

    Writes JSON to docs/plans/_debug/<plan_id>/<tier>-<stamp>.json
    with the reproduce command and link to the log file.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    debug_dir = plans_dir / "_debug" / plan_id
    debug_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = debug_dir / f"{tier}-{stamp}.json"

    fixture = {
        "plan_id": plan_id,
        "tier": tier,
        "command": command,
        "reproduce": command,
        "log_path": log_path,
        "recorded_at": _now_iso(),
    }
    fixture_path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    return str(fixture_path)

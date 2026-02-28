"""State file management for per-plan execution tracking.

State files live at docs/plans/_state/<plan_id>.json.
The bridge creates them as "pending". Codex owns all other transitions.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any, Iterator

from .log import get_logger
from .schemas import (
    DEFAULT_LOCK_LEASE_SECONDS,
    VALID_SEVERITIES,
    VALID_TRANSITIONS,
    PlanState,
    _now_iso,
    validate_schema_version,
)

_log = get_logger("state")


PATCHABLE_FIELDS = {
    "status",
    "completed_tasks",
    "last_run_at",
    "verification",
    "executor",
    "blocked_reason",
    "blocked_severity",
    "failure_reason",
    "lock",
    "recovery_notes",
    "cursor",
    "verify_quick",
    "last_error_output_path",
    "git_sha_before",
    "git_sha_after",
    "touched_paths",
}


def init_state(
    plan_id: str,
    source_path: str,
    source_hash: str,
    total_tasks: int,
) -> PlanState:
    """Create a new PlanState in pending status."""
    return PlanState(
        plan_id=plan_id,
        source_plan_path=source_path,
        source_plan_hash=source_hash,
        status="pending",
        total_tasks=total_tasks,
    )


def read_state(state_path: Path) -> PlanState | None:
    """Read a state file. Returns None if file doesn't exist."""
    if not state_path.exists():
        _log.debug("state_miss: %s", state_path.name)
        return None
    _log.debug("state_read: %s", state_path.name)
    text = state_path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid state JSON at {state_path}: {exc}") from exc
    warning = validate_schema_version(raw)
    if warning:
        import sys

        print(f"WARNING: {state_path.name}: {warning}", file=sys.stderr)
    try:
        return PlanState.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid state schema at {state_path}: {exc}") from exc


def write_state(state_path: Path, state: PlanState) -> None:
    """Write a state file, creating parent directories if needed."""
    _log.debug("state_write: %s status=%s", state_path.name, state.status)
    state.updated_at = _now_iso()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_json() + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(state_path.parent),
        prefix=f".{state_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@contextmanager
def state_lock(
    state_path: Path,
    *,
    timeout_seconds: float = 5.0,
    retry_interval_seconds: float = 0.05,
    lease_seconds: float = DEFAULT_LOCK_LEASE_SECONDS,
) -> Iterator[None]:
    """Acquire a per-state lock via atomic lockfile creation.

    Lock files include lease metadata and are recovered if stale.
    """
    lock_path = state_path.with_suffix(f"{state_path.suffix}.lock")
    deadline = _now_epoch() + timeout_seconds
    owner_id = uuid.uuid4().hex

    _log.debug("lock_acquire: %s", lock_path.name)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = {
                "owner_id": owner_id,
                "acquired_at": _now_iso(),
                "expires_at": _to_iso(_now_epoch() + max(lease_seconds, 0.001)),
            }
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.close(fd)
            break
        except FileExistsError:
            if _is_stale_lock(lock_path, lease_seconds):
                _break_stale_lock(lock_path)
                continue
            if _now_epoch() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}") from None
            sleep(retry_interval_seconds)

    _log.debug("lock_acquired: %s owner=%s", lock_path.name, owner_id[:8])
    try:
        yield
    finally:
        _release_lock(lock_path, owner_id)
        _log.debug("lock_released: %s", lock_path.name)


def transition(state: PlanState, new_status: str, **kwargs: object) -> PlanState:
    """Transition a plan to a new status with validation.

    Raises ValueError if the transition is not allowed.
    Additional kwargs are set on the state (e.g., executor, blocked_reason).
    """
    _log.debug("transition: %s %s -> %s", state.plan_id, state.status, new_status)
    allowed = VALID_TRANSITIONS.get(state.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {state.status} → {new_status}. "
            f"Allowed: {sorted(allowed)}"
        )

    state.status = new_status
    state.updated_at = _now_iso()

    if new_status == "executing":
        state.last_run_at = _now_iso()
    elif new_status in {"verified", "failed"}:
        state.lock = None

    for key, value in kwargs.items():
        if hasattr(state, key):
            setattr(state, key, value)

    return state


def complete_task(state: PlanState, task_num: int) -> PlanState:
    """Mark a task as completed in the state and advance cursor."""
    if task_num not in state.completed_tasks:
        state.completed_tasks.append(task_num)
        state.completed_tasks.sort()

    next_task = task_num + 1
    if next_task <= state.total_tasks:
        state.cursor = {"task": next_task, "step": 0}
    else:
        state.cursor = {"task": task_num, "step": -1}  # -1 signals all done

    state.updated_at = _now_iso()
    return state


def advance_step(state: PlanState, step: int) -> PlanState:
    """Advance cursor to a specific step within the current task."""
    if state.cursor is None:
        state.cursor = {"task": 1, "step": step}
    else:
        state.cursor["step"] = step
    state.updated_at = _now_iso()
    return state


def record_verification(
    state: PlanState,
    tier: str,
    command: str,
    exit_code: int,
    log_path: str = "",
) -> PlanState:
    """Record a verification result for a tier (quick/full/smoke)."""
    if state.verification is None:
        state.verification = {}
    existing = state.verification.get(tier, [])

    # Backward compatibility: old schema used a single dict per tier.
    if isinstance(existing, dict):
        existing_list: list[dict[str, object]] = [existing]
    elif isinstance(existing, list):
        existing_list = existing
    else:
        existing_list = []

    existing_list.append(
        {
            "command": command,
            "exit_code": exit_code,
            "log_path": log_path,
            "ran_at": _now_iso(),
        }
    )
    state.verification[tier] = existing_list
    state.updated_at = _now_iso()
    return state


def acquire_lease(
    state: PlanState,
    run_id: str | None = None,
    lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
) -> PlanState:
    """Acquire a logical lease on the plan.

    If an existing lease has expired, records a recovery note and takes over.
    Raises ValueError if an active (non-expired) lease exists.
    """
    now = _now_iso()
    now_epoch = _now_epoch()
    actual_run_id = run_id or str(uuid.uuid4())

    if state.lock is not None:
        expires_at = state.lock.get("expires_at", "")
        expires_dt: datetime | None = None
        if isinstance(expires_at, str):
            try:
                expires_dt = datetime.fromisoformat(expires_at)
            except ValueError:
                expires_dt = None

        if expires_dt and expires_dt.timestamp() > now_epoch:
            raise ValueError(
                f"Plan {state.plan_id} is already leased by "
                f"run_id={state.lock.get('run_id')} until {expires_at}"
            )

        state.recovery_notes.append(
            {
                "event": "lease_expired_takeover",
                "previous_run_id": state.lock.get("run_id", "unknown"),
                "previous_acquired_at": state.lock.get("acquired_at", ""),
                "previous_expires_at": expires_at,
                "taken_over_at": now,
                "new_run_id": actual_run_id,
            }
        )

    expires_at_iso = datetime.fromtimestamp(
        now_epoch + lease_seconds, tz=timezone.utc
    ).isoformat()

    state.lock = {
        "run_id": actual_run_id,
        "acquired_at": now,
        "expires_at": expires_at_iso,
    }
    state.updated_at = now
    return state


def renew_lease(
    state: PlanState,
    run_id: str,
    lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
) -> PlanState:
    """Renew the lease for the current holder. Raises if run_id does not match."""
    if state.lock is None:
        raise ValueError(f"No active lease on plan {state.plan_id}")
    if state.lock.get("run_id") != run_id:
        raise ValueError(f"Lease held by {state.lock.get('run_id')}, not {run_id}")
    now_epoch = _now_epoch()
    state.lock["expires_at"] = datetime.fromtimestamp(
        now_epoch + lease_seconds, tz=timezone.utc
    ).isoformat()
    state.updated_at = _now_iso()
    return state


def release_lease(state: PlanState) -> PlanState:
    """Clear the lease. Called on verified/failed transitions."""
    state.lock = None
    state.updated_at = _now_iso()
    return state


def apply_state_patch(state: PlanState, patch: dict[str, Any]) -> PlanState:
    """Apply validated patch updates to a state object."""
    unknown = sorted(set(patch) - PATCHABLE_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported state patch fields: {unknown}")

    status = patch.get("status")
    if status is not None:
        if not isinstance(status, str):
            raise ValueError("status must be a string")
        if status != state.status:
            transition(state, status)

    if "completed_tasks" in patch:
        state.completed_tasks = _validate_completed_tasks(
            patch["completed_tasks"],
            state.total_tasks,
        )
    if "last_run_at" in patch:
        state.last_run_at = _validate_optional_str("last_run_at", patch["last_run_at"])
    if "verification" in patch:
        state.verification = _validate_verification(patch["verification"])
    if "executor" in patch:
        state.executor = _validate_optional_dict("executor", patch["executor"])
    if "blocked_reason" in patch:
        state.blocked_reason = _validate_optional_str(
            "blocked_reason",
            patch["blocked_reason"],
        )
    if "blocked_severity" in patch:
        severity = _validate_optional_str("blocked_severity", patch["blocked_severity"])
        if severity is not None and severity not in VALID_SEVERITIES:
            raise ValueError(
                f"blocked_severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}"
            )
        state.blocked_severity = severity
    if "failure_reason" in patch:
        state.failure_reason = _validate_optional_str(
            "failure_reason",
            patch["failure_reason"],
        )
    if "lock" in patch:
        state.lock = _validate_optional_dict("lock", patch["lock"])
    if "recovery_notes" in patch:
        state.recovery_notes = _validate_recovery_notes(patch["recovery_notes"])
    if "cursor" in patch:
        state.cursor = _validate_cursor(patch["cursor"])
    if "verify_quick" in patch:
        state.verify_quick = _validate_optional_str("verify_quick", patch["verify_quick"])
    if "last_error_output_path" in patch:
        state.last_error_output_path = _validate_optional_str(
            "last_error_output_path", patch["last_error_output_path"]
        )
    if "git_sha_before" in patch:
        state.git_sha_before = _validate_optional_str("git_sha_before", patch["git_sha_before"])
    if "git_sha_after" in patch:
        state.git_sha_after = _validate_optional_str("git_sha_after", patch["git_sha_after"])
    if "touched_paths" in patch:
        if not isinstance(patch["touched_paths"], list):
            raise ValueError("touched_paths must be a list")
        state.touched_paths = [str(p) for p in patch["touched_paths"]]

    state.updated_at = _now_iso()
    return state


def _is_stale_lock(lock_path: Path, lease_seconds: float) -> bool:
    payload = _read_lock_payload(lock_path)
    if payload:
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, str):
            try:
                return _now_epoch() >= datetime.fromisoformat(expires_at).timestamp()
            except ValueError:
                return True
        return True

    try:
        age = _now_epoch() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age >= max(lease_seconds, 0.001)


def _break_stale_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


def _release_lock(lock_path: Path, owner_id: str) -> None:
    payload = _read_lock_payload(lock_path)
    if payload and payload.get("owner_id") != owner_id:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


def _read_lock_payload(lock_path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _validate_completed_tasks(value: Any, total_tasks: int) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("completed_tasks must be a list of positive integers")
    tasks: set[int] = set()
    for item in value:
        if not isinstance(item, int) or item < 1:
            raise ValueError("completed_tasks must be a list of positive integers")
        if total_tasks and item > total_tasks:
            raise ValueError(
                f"completed_tasks entry {item} exceeds total_tasks={total_tasks}"
            )
        tasks.add(item)
    return sorted(tasks)


def _validate_optional_str(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _validate_optional_dict(field: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object or null")
    return value


def _validate_verification(value: Any) -> dict[str, list[dict[str, Any]]] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("verification must be an object")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for tier, results in value.items():
        if not isinstance(tier, str):
            raise ValueError("verification tiers must be strings")
        if isinstance(results, dict):
            results_list: list[dict[str, Any]] = [results]
        elif isinstance(results, list):
            results_list = []
            for result in results:
                if not isinstance(result, dict):
                    raise ValueError("verification entries must be objects")
                results_list.append(result)
        else:
            raise ValueError("verification entries must be objects or list of objects")
        normalized[tier] = results_list
    return normalized


def _validate_recovery_notes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("recovery_notes must be a list of objects")
    notes: list[dict[str, Any]] = []
    for note in value:
        if not isinstance(note, dict):
            raise ValueError("recovery_notes must be a list of objects")
        notes.append(note)
    return notes


def _validate_cursor(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("cursor must be an object or null")
    task = value.get("task")
    step = value.get("step")
    if task is not None and (not isinstance(task, int) or task < 0):
        raise ValueError("cursor.task must be a non-negative integer")
    if step is not None and (not isinstance(step, int) or step < -1):
        raise ValueError("cursor.step must be >= -1")
    out: dict[str, int] = {}
    if isinstance(task, int):
        out["task"] = task
    if isinstance(step, int):
        out["step"] = step
    return out


def _to_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _now_epoch() -> float:
    return time.time()

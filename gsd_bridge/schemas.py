"""Data models for bridge state, manifest, and reconciliation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Valid status values and transitions
# ---------------------------------------------------------------------------

VALID_STATUSES = {"pending", "executing", "blocked", "verified", "failed"}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"executing"},
    "executing": {"verified", "failed", "blocked"},
    "blocked": {"executing"},   # resume
    "failed": {"executing"},    # retry
}

CURRENT_SCHEMA_VERSION = "3.1"
DEFAULT_LOCK_LEASE_SECONDS = 3600   # 1 hour
VALID_SEVERITIES = {"critical", "high", "medium", "low"}


# ---------------------------------------------------------------------------
# PlanState — canonical per-plan state artifact
# ---------------------------------------------------------------------------

@dataclass
class PlanState:
    plan_id: str
    source_plan_path: str
    source_plan_hash: str
    status: str                                     # pending | executing | blocked | verified | failed
    completed_tasks: list[int] = field(default_factory=list)
    total_tasks: int = 0
    last_run_at: str | None = None
    verification: dict[str, list[dict[str, Any]]] | None = None  # {quick: [{...}], ...}
    executor: dict[str, Any] | None = None           # {tool, model, run_id}
    blocked_reason: str | None = None
    blocked_severity: str | None = None             # critical|high|medium|low
    failure_reason: str | None = None
    lock: dict[str, str] | None = None              # {run_id, acquired_at, expires_at}
    recovery_notes: list[dict[str, Any]] = field(default_factory=list)
    cursor: dict[str, int] | None = None            # {task, step}
    verify_quick: str | None = None                 # exact reproduce command
    last_error_output_path: str | None = None       # path to last failed verify log
    git_sha_before: str | None = None               # git SHA when execution started
    git_sha_after: str | None = None                # git SHA when verified
    touched_paths: list[str] = field(default_factory=list)  # files changed (diffstat)
    schema_version: str = CURRENT_SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlanState:
        payload = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**migrate_legacy_plan_state(payload))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> PlanState:
        return cls.from_dict(json.loads(text))


# ---------------------------------------------------------------------------
# ManifestEntry — one plan in the ordered manifest
# ---------------------------------------------------------------------------

@dataclass
class ManifestEntry:
    plan_id: str
    wave: int
    phase: str
    plan_number: int
    priority: int                                    # wave * 1000 + phase_num * 10 + plan_num
    plan_path: str                                   # docs/plans/<plan_id>.md
    state_path: str                                  # docs/plans/_state/<plan_id>.json
    source_path: str                                 # .planning/phases/.../XX-NN-PLAN.md
    source_hash: str
    depends_on: list[str] = field(default_factory=list)
    batch_size: int = 3
    batching: list[list[int]] | None = None
    status: str = "pending"
    execution_contract: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ManifestEntry:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Manifest — ordered collection of plans (Codex entrypoint)
# ---------------------------------------------------------------------------

@dataclass
class Manifest:
    version: str = CURRENT_SCHEMA_VERSION
    generated_at: str = ""
    project_root: str = ""
    plans: list[ManifestEntry] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Manifest:
        plans_raw = d.pop("plans", [])
        m = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        m.plans = [ManifestEntry.from_dict(p) for p in plans_raw]
        return m

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        return cls.from_dict(json.loads(text))

    def compute_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {s: 0 for s in VALID_STATUSES}
        counts["no-state"] = 0
        counts["total"] = len(self.plans)
        for p in self.plans:
            if p.status in counts:
                counts[p.status] += 1
            else:
                counts["no-state"] += 1
        self.summary = counts
        return counts


# ---------------------------------------------------------------------------
# ReconcileReport — output of post-execution reconciliation
# ---------------------------------------------------------------------------

@dataclass
class DriftWarning:
    plan_id: str
    source_path: str
    expected_hash: str
    actual_hash: str


@dataclass
class ReconcileIssue:
    plan_id: str
    issue_type: str      # "verification_missing" | "stale_execution" | "drift"
    description: str


@dataclass
class ReconcileReport:
    generated_at: str = ""
    plan_states: list[PlanState] = field(default_factory=list)
    drift_warnings: list[DriftWarning] = field(default_factory=list)
    issues: list[ReconcileIssue] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = _now_iso()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple of ints."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def migrate_legacy_plan_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy plan-state fields into canonical v3 shape."""
    normalized = dict(payload)

    # --- v1 -> v2 migrations ---
    verification = normalized.get("verification")
    if isinstance(verification, dict):
        normalized_verification: dict[str, list[dict[str, Any]]] = {}
        for tier, results in verification.items():
            if isinstance(results, list):
                normalized_verification[tier] = results
            elif isinstance(results, dict):
                normalized_verification[tier] = [results]
            else:
                normalized_verification[tier] = []
        normalized["verification"] = normalized_verification

    if normalized.get("status") == "failed":
        blocked_reason = normalized.get("blocked_reason")
        failure_reason = normalized.get("failure_reason")
        if not failure_reason and blocked_reason:
            normalized["failure_reason"] = blocked_reason
            normalized["blocked_reason"] = None

    # --- v2 -> v3 migrations ---
    version = normalized.get("schema_version", "2.0")
    if _parse_version(version) < _parse_version(CURRENT_SCHEMA_VERSION):
        normalized.setdefault("schema_version", CURRENT_SCHEMA_VERSION)
        normalized.setdefault("lock", None)
        normalized.setdefault("recovery_notes", [])
        normalized.setdefault("cursor", None)
        normalized.setdefault("blocked_severity", None)
        normalized.setdefault("verify_quick", None)
        normalized.setdefault("last_error_output_path", None)
        normalized.setdefault("git_sha_before", None)
        normalized.setdefault("git_sha_after", None)
        normalized.setdefault("touched_paths", [])

        # Backfill ran_at on existing verification results
        if isinstance(normalized.get("verification"), dict):
            for _tier, results in normalized["verification"].items():
                if isinstance(results, list):
                    for result in results:
                        if isinstance(result, dict) and "ran_at" not in result:
                            result["ran_at"] = ""

        normalized["schema_version"] = CURRENT_SCHEMA_VERSION

    return normalized


def validate_schema_version(payload: dict[str, Any]) -> str | None:
    """Return a warning string if schema_version is outdated, else None."""
    version = payload.get("schema_version", "2.0")
    if _parse_version(version) < _parse_version(CURRENT_SCHEMA_VERSION):
        return (
            f"State file at schema v{version}, current is v{CURRENT_SCHEMA_VERSION}. "
            f"Run 'gsd-bridge migrate' to update."
        )
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

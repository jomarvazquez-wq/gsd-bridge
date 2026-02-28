from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gsd_bridge import cli
from gsd_bridge.codex_adapter import (
    advance_step,
    get_next_plan,
    mark_blocked,
    mark_failed,
    mark_verified,
    record_verification,
    renew_lock,
    resume_execution,
    start_execution,
)
from gsd_bridge.manifest import read_manifest, write_manifest
from gsd_bridge.parser import validate_plan
from gsd_bridge.plan_id import content_hash
from gsd_bridge.reconcile import _tier_status_icon, reconcile
from gsd_bridge.schemas import (
    CURRENT_SCHEMA_VERSION,
    Manifest,
    ManifestEntry,
    validate_schema_version,
)
from gsd_bridge.state import init_state, read_state, write_state


def _entry(
    plan_id: str,
    *,
    wave: int = 1,
    plan_number: int = 1,
    status: str = "pending",
    depends_on: list[str] | None = None,
) -> ManifestEntry:
    return ManifestEntry(
        plan_id=plan_id,
        wave=wave,
        phase=f"{wave:02d}-phase",
        plan_number=plan_number,
        priority=wave * 1000 + plan_number,
        plan_path=f"docs/plans/{plan_id}.md",
        state_path=f"docs/plans/_state/{plan_id}.json",
        source_path=f".planning/phases/{wave:02d}/{wave:02d}-{plan_number:02d}-PLAN.md",
        source_hash=f"hash-{plan_id}",
        status=status,
        depends_on=depends_on or [],
    )


class BridgeTests(unittest.TestCase):
    def test_adapter_subcommand_next_plan_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            plan_id = "plan-a"
            state = init_state(plan_id, ".planning/a.md", "abc", total_tasks=1)
            write_state(state_dir / f"{plan_id}.json", state)

            manifest = Manifest(
                project_root=str(root),
                plans=[_entry(plan_id)],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            output = io.StringIO()
            with redirect_stdout(output):
                rc = cli.main(["adapter", "next-plan", str(manifest_path)])
            self.assertEqual(rc, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["plan_id"], plan_id)

            state.status = "blocked"
            write_state(state_dir / f"{plan_id}.json", state)
            with redirect_stdout(io.StringIO()):
                rc = cli.main(["adapter", "resume", str(state_dir / f'{plan_id}.json')])
            self.assertEqual(rc, 0)
            resumed = read_state(state_dir / f"{plan_id}.json")
            assert resumed is not None
            self.assertEqual(resumed.status, "executing")

    def test_get_next_plan_enforces_wave_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("wave1-a", wave=1, plan_number=1),
                    _entry("wave2-a", wave=2, plan_number=1),
                ],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            s1 = init_state("wave1-a", ".planning/a.md", "a", total_tasks=1)
            s1.status = "failed"
            write_state(state_dir / "wave1-a.json", s1)

            s2 = init_state("wave2-a", ".planning/b.md", "b", total_tasks=1)
            write_state(state_dir / "wave2-a.json", s2)

            self.assertIsNone(get_next_plan(manifest_path))

    def test_get_next_plan_enforces_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("wave1-done", wave=1, plan_number=1, status="verified"),
                    _entry("blocked-by-dep", wave=2, plan_number=1, depends_on=["ready-first"]),
                    _entry("ready-first", wave=2, plan_number=2),
                ],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            s1 = init_state("wave1-done", ".planning/a.md", "a", total_tasks=1)
            s1.status = "verified"
            write_state(state_dir / "wave1-done.json", s1)

            s2 = init_state("blocked-by-dep", ".planning/b.md", "b", total_tasks=1)
            write_state(state_dir / "blocked-by-dep.json", s2)

            s3 = init_state("ready-first", ".planning/c.md", "c", total_tasks=1)
            write_state(state_dir / "ready-first.json", s3)

            next_plan = get_next_plan(manifest_path)
            assert next_plan is not None
            self.assertEqual(next_plan.plan_id, "ready-first")

    def test_record_verification_accumulates_and_creates_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "docs" / "plans" / "_state"
            state_dir.mkdir(parents=True)

            state_path = state_dir / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            record_verification(state_path, "quick", "npx tsc --noEmit", 0)
            record_verification(state_path, "quick", "npx eslint .", 0)

            updated = read_state(state_path)
            assert updated is not None
            self.assertIsNotNone(updated.verification)
            quick = updated.verification["quick"]
            self.assertEqual(len(quick), 2)
            for item in quick:
                self.assertTrue(item["log_path"])
                log_path = root / item["log_path"]
                self.assertTrue(log_path.exists())

    def test_mark_failed_uses_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            write_state(state_path, state)

            mark_failed(state_path, "verification failed")

            updated = read_state(state_path)
            assert updated is not None
            self.assertEqual(updated.failure_reason, "verification failed")
            self.assertIsNone(updated.blocked_reason)

    def test_read_state_migrates_legacy_verification_dict_to_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_state = {
                "plan_id": "plan-a",
                "source_plan_path": ".planning/a.md",
                "source_plan_hash": "abc",
                "status": "executing",
                "total_tasks": 1,
                "verification": {
                    "quick": {
                        "command": "npx tsc --noEmit",
                        "exit_code": 0,
                        "log_path": "docs/plans/_logs/plan-a/quick.log",
                    }
                },
            }
            state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

            updated = read_state(state_path)
            assert updated is not None
            assert updated.verification is not None
            quick = updated.verification["quick"]
            self.assertIsInstance(quick, list)
            self.assertEqual(len(quick), 1)
            self.assertEqual(quick[0]["command"], "npx tsc --noEmit")

    def test_read_state_migrates_failed_blocked_reason_to_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_state = {
                "plan_id": "plan-a",
                "source_plan_path": ".planning/a.md",
                "source_plan_hash": "abc",
                "status": "failed",
                "total_tasks": 1,
                "blocked_reason": "verification failed",
                "failure_reason": None,
            }
            state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

            updated = read_state(state_path)
            assert updated is not None
            self.assertEqual(updated.failure_reason, "verification failed")
            self.assertIsNone(updated.blocked_reason)

    def test_read_manifest_has_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "_manifest.json"
            manifest_path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid manifest JSON"):
                read_manifest(manifest_path)

    def test_compute_summary_counts_no_state(self) -> None:
        manifest = Manifest(
            project_root="/tmp",
            plans=[
                _entry("a", status="pending"),
                _entry("b", status="no-state"),
            ],
        )
        summary = manifest.compute_summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["no-state"], 1)

    def test_find_project_root_fallback_prefers_input_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp) / "docs" / "plans"
            plans_dir.mkdir(parents=True)
            found = cli._find_project_root(plans_dir)
            self.assertEqual(found, plans_dir.resolve())
            self.assertNotEqual(found, Path.cwd())

    def test_reconcile_accepts_custom_stale_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(project_root=str(root), plans=[_entry("plan-a")])
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            state.last_run_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            write_state(state_dir / "plan-a.json", state)

            report = reconcile(manifest_path, root, stale_threshold_hours=1)
            issue_types = [issue.issue_type for issue in report.issues]
            self.assertIn("stale_execution", issue_types)

    def test_validate_plan_requires_success_criteria(self) -> None:
        parsed = {
            "frontmatter": {"phase": "01-foundation", "plan": 1},
            "objective": "x",
            "tasks": [{"name": "x"}],
            "verification": "x",
            "success_criteria": "",
            "execution_contract": None,
        }
        errors = validate_plan(parsed)
        self.assertIn("Missing <success_criteria> tag", errors)

    def test_cmd_reconcile_syncs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(project_root=str(root), plans=[_entry("plan-a", status="pending")])
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "verified"
            write_state(state_dir / "plan-a.json", state)

            rc = cli.cmd_reconcile(argparse.Namespace(plans_dir=plans_dir, stale_hours=24))
            self.assertEqual(rc, 0)

            refreshed = read_manifest(manifest_path)
            self.assertEqual(refreshed.plans[0].status, "verified")

    def test_refresh_command_syncs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(project_root=str(root), plans=[_entry("plan-a", status="pending")])
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "verified"
            write_state(state_dir / "plan-a.json", state)

            rc = cli.main(["refresh", str(plans_dir)])
            self.assertEqual(rc, 0)

            refreshed = read_manifest(manifest_path)
            self.assertEqual(refreshed.plans[0].status, "verified")

    def test_archive_command_moves_plan_and_updates_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            plan_id = "plan-a"
            manifest = Manifest(project_root=str(root), plans=[_entry(plan_id, status="verified")])
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            (plans_dir / f"{plan_id}.md").write_text("# plan", encoding="utf-8")
            state = init_state(plan_id, ".planning/a.md", "abc", total_tasks=1)
            state.status = "verified"
            write_state(state_dir / f"{plan_id}.json", state)
            logs_dir = plans_dir / "_logs" / plan_id
            logs_dir.mkdir(parents=True)
            (logs_dir / "quick.log").write_text("ok", encoding="utf-8")

            rc = cli.main(["archive", str(plans_dir), plan_id])
            self.assertEqual(rc, 0)

            refreshed = read_manifest(manifest_path)
            self.assertEqual(len(refreshed.plans), 0)
            self.assertTrue((plans_dir / "_archive" / plan_id / f"{plan_id}.md").exists())
            self.assertTrue((plans_dir / "_archive" / plan_id / f"{plan_id}.json").exists())
            self.assertTrue((plans_dir / "_archive" / plan_id / "logs" / "quick.log").exists())

    def test_adapter_rollback_runs_contract_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            plan_id = "plan-a"
            manifest = Manifest(
                project_root=str(root),
                plans=[
                    ManifestEntry(
                        plan_id=plan_id,
                        wave=1,
                        phase="01-phase",
                        plan_number=1,
                        priority=1001,
                        plan_path=f"docs/plans/{plan_id}.md",
                        state_path=f"docs/plans/_state/{plan_id}.json",
                        source_path=".planning/phases/01/01-01-PLAN.md",
                        source_hash="hash-a",
                        status="failed",
                        execution_contract={"rollback": {"argv": ["echo", "rollback"]}},
                    )
                ],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            state = init_state(plan_id, ".planning/a.md", "abc", total_tasks=1)
            state.status = "failed"
            write_state(state_dir / f"{plan_id}.json", state)

            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(
                    ["adapter", "rollback", str(manifest_path), str(state_dir / f"{plan_id}.json")]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["argv"][:2], ["echo", "rollback"])
            self.assertTrue((root / payload["log_path"]).exists())


    # ---------------------------------------------------------------
    # Feature 5: Schema Versioning + Migration
    # ---------------------------------------------------------------

    def test_migrate_v2_state_to_v3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            v2_state = {
                "plan_id": "plan-a",
                "source_plan_path": ".planning/a.md",
                "source_plan_hash": "abc",
                "status": "pending",
                "total_tasks": 3,
            }
            state_path.write_text(json.dumps(v2_state), encoding="utf-8")

            loaded = read_state(state_path)
            assert loaded is not None
            self.assertEqual(loaded.schema_version, CURRENT_SCHEMA_VERSION)
            self.assertIsNone(loaded.lock)
            self.assertEqual(loaded.recovery_notes, [])
            self.assertIsNone(loaded.cursor)
            self.assertIsNone(loaded.blocked_severity)

    def test_validate_schema_version_warns_on_old(self) -> None:
        warning = validate_schema_version({"schema_version": "2.0"})
        self.assertIsNotNone(warning)
        self.assertIn("v2.0", warning)

    def test_validate_schema_version_ok_on_current(self) -> None:
        warning = validate_schema_version({"schema_version": CURRENT_SCHEMA_VERSION})
        self.assertIsNone(warning)

    def test_parse_version_handles_multi_digit_major(self) -> None:
        from gsd_bridge.schemas import _parse_version
        self.assertGreater(_parse_version("10.0"), _parse_version("9.0"))
        self.assertGreater(_parse_version("2.10"), _parse_version("2.9"))
        self.assertEqual(_parse_version("3.0"), _parse_version("3.0"))
        self.assertEqual(_parse_version("invalid"), (0,))

    def test_cmd_migrate_rewrites_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            v2_state = {
                "plan_id": "plan-a",
                "source_plan_path": ".planning/a.md",
                "source_plan_hash": "abc",
                "status": "pending",
                "total_tasks": 1,
            }
            (state_dir / "plan-a.json").write_text(json.dumps(v2_state), encoding="utf-8")

            manifest = Manifest(version="2.0", project_root=str(root), plans=[_entry("plan-a")])
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            rc = cli.cmd_migrate(argparse.Namespace(plans_dir=plans_dir))
            self.assertEqual(rc, 0)

            reloaded = read_state(state_dir / "plan-a.json")
            assert reloaded is not None
            self.assertEqual(reloaded.schema_version, CURRENT_SCHEMA_VERSION)

            refreshed_manifest = read_manifest(plans_dir / "_manifest.json")
            self.assertEqual(refreshed_manifest.version, CURRENT_SCHEMA_VERSION)

    # ---------------------------------------------------------------
    # Feature 3: Structured Verification Stages
    # ---------------------------------------------------------------

    def test_record_verification_includes_ran_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "docs" / "plans" / "_state"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            record_verification(state_path, "quick", "npx tsc --noEmit", 0)
            updated = read_state(state_path)
            assert updated is not None
            assert updated.verification is not None
            quick = updated.verification["quick"]
            self.assertEqual(len(quick), 1)
            self.assertIn("ran_at", quick[0])
            self.assertTrue(len(quick[0]["ran_at"]) > 0)

    def test_tier_status_icon(self) -> None:
        self.assertEqual(_tier_status_icon(None), "---")
        self.assertEqual(_tier_status_icon([]), "---")
        self.assertEqual(_tier_status_icon([{"exit_code": 0}]), "PASS")
        self.assertEqual(_tier_status_icon([{"exit_code": 1}]), "FAIL")
        self.assertEqual(
            _tier_status_icon([{"exit_code": 1}, {"exit_code": 0}]),
            "PASS",
        )

    # ---------------------------------------------------------------
    # Feature 4: Standardized Blocker Packet
    # ---------------------------------------------------------------

    def test_mark_blocked_with_severity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            write_state(state_path, state)

            result = mark_blocked(
                state_path,
                "Need API key",
                severity="critical",
                who_must_answer="@jomar",
                resume_command="gsd-bridge adapter resume plan-a",
            )
            self.assertEqual(result.blocked_severity, "critical")
            self.assertEqual(result.blocked_reason, "Need API key")

            blocker_path = Path(tmp) / "docs" / "plans" / "_blockers" / "plan-a.md"
            self.assertTrue(blocker_path.exists())
            content = blocker_path.read_text(encoding="utf-8")
            self.assertIn("**CRITICAL**", content)
            self.assertIn("@jomar", content)
            self.assertIn("Resume command", content)

    def test_mark_blocked_invalid_severity_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            write_state(state_path, state)

            result = mark_blocked(state_path, "something", severity="bogus")
            self.assertEqual(result.blocked_severity, "high")

    # ---------------------------------------------------------------
    # Feature 2: Resume Cursor
    # ---------------------------------------------------------------

    def test_start_execution_initializes_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=3)
            write_state(state_path, state)

            result = start_execution(state_path, {"run_id": "test-1"})
            self.assertEqual(result.cursor, {"task": 1, "step": 0})

    def test_complete_task_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=2)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "test-1"})
            from gsd_bridge.codex_adapter import complete_task
            complete_task(state_path, 1)
            updated = read_state(state_path)
            assert updated is not None
            self.assertEqual(updated.cursor, {"task": 2, "step": 0})

            complete_task(state_path, 2)
            final = read_state(state_path)
            assert final is not None
            self.assertEqual(final.cursor, {"task": 2, "step": -1})

    def test_complete_task_requires_executing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from gsd_bridge.codex_adapter import complete_task

            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=2)
            write_state(state_path, state)  # pending

            with self.assertRaisesRegex(ValueError, "executing"):
                complete_task(state_path, 1)

    def test_advance_step_requires_executing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=2)
            write_state(state_path, state)  # pending

            with self.assertRaisesRegex(ValueError, "executing"):
                advance_step(state_path, 1)

    def test_record_verification_requires_executing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)  # pending

            with self.assertRaisesRegex(ValueError, "executing"):
                record_verification(state_path, "quick", "python -m unittest -v", 0)

    def test_resume_preserves_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=3)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "test-1"})
            from gsd_bridge.codex_adapter import complete_task
            complete_task(state_path, 1)

            mark_blocked(state_path, "waiting for info")
            resumed = resume_execution(state_path, {"run_id": "test-2"})
            self.assertEqual(resumed.cursor, {"task": 2, "step": 0})

    def test_advance_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=3)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "test-1"})
            result = advance_step(state_path, 3)
            self.assertEqual(result.cursor, {"task": 1, "step": 3})

    # ---------------------------------------------------------------
    # Feature 1: Lock + Lease + Recovery
    # ---------------------------------------------------------------

    def test_start_execution_acquires_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            result = start_execution(state_path, {"run_id": "run-1"})
            self.assertIsNotNone(result.lock)
            self.assertEqual(result.lock["run_id"], "run-1")
            self.assertIn("acquired_at", result.lock)
            self.assertIn("expires_at", result.lock)

            expires = datetime.fromisoformat(result.lock["expires_at"])
            now = datetime.now(timezone.utc)
            self.assertGreater(expires, now)
            self.assertLess((expires - now).total_seconds(), 3700)

    def test_start_execution_blocks_on_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / ".planning" / "a.md"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("new-content", encoding="utf-8")

            state_path = root / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state(
                "plan-a",
                ".planning/a.md",
                content_hash("old-content"),
                total_tasks=1,
            )
            write_state(state_path, state)

            with self.assertRaisesRegex(ValueError, "drift"):
                start_execution(state_path, {"run_id": "run-1"})

    def test_mark_verified_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from gsd_bridge.codex_adapter import complete_task

            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            complete_task(state_path, 1)
            record_verification(state_path, "quick", "python -m unittest -v", 0)
            result = mark_verified(state_path)
            self.assertIsNone(result.lock)

    def test_mark_verified_requires_all_tasks_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=2)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            with self.assertRaisesRegex(ValueError, "completed"):
                mark_verified(state_path)

    def test_mark_verified_requires_verification_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from gsd_bridge.codex_adapter import complete_task

            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            complete_task(state_path, 1)
            with self.assertRaisesRegex(ValueError, "verification"):
                mark_verified(state_path)

    def test_mark_failed_releases_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            result = mark_failed(state_path, "test failure")
            self.assertIsNone(result.lock)

    def test_renew_lock_extends_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            started = start_execution(state_path, {"run_id": "run-1"})
            original_expires = started.lock["expires_at"]

            import time
            time.sleep(0.1)

            renewed = renew_lock(state_path, "run-1")
            self.assertGreater(renewed.lock["expires_at"], original_expires)

    def test_renew_lock_rejects_wrong_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_path, state)

            start_execution(state_path, {"run_id": "run-1"})
            with self.assertRaises(ValueError):
                renew_lock(state_path, "wrong-run-id")

    def test_expired_lock_allows_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            state.lock = {
                "run_id": "dead-run",
                "acquired_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            }
            write_state(state_path, state)

            # Mark as blocked first so we can resume (executing can't go to executing)
            mark_blocked(state_path, "stuck")
            result = resume_execution(state_path, {"run_id": "new-run"})

            self.assertEqual(result.lock["run_id"], "new-run")
            self.assertTrue(len(result.recovery_notes) >= 1)
            self.assertEqual(
                result.recovery_notes[-1]["previous_run_id"], "dead-run"
            )

    def test_reconcile_detects_expired_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(project_root=str(root), plans=[_entry("plan-a")])
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            state.last_run_at = datetime.now(timezone.utc).isoformat()
            state.lock = {
                "run_id": "dead-run",
                "acquired_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            }
            write_state(state_dir / "plan-a.json", state)

            report = reconcile(plans_dir / "_manifest.json", root)
            issue_types = [i.issue_type for i in report.issues]
            self.assertIn("expired_lock", issue_types)

    # ---------------------------------------------------------------
    # Step 7: Reconcile gap tests
    # ---------------------------------------------------------------

    def test_generate_status_md_drift_section(self) -> None:
        from gsd_bridge.reconcile import generate_status_md
        from gsd_bridge.schemas import DriftWarning, ReconcileIssue, ReconcileReport

        report = ReconcileReport()
        report.summary = {"total": 1, "verified": 0}
        report.drift_warnings = [
            DriftWarning(
                plan_id="plan-drift",
                source_path=".planning/phases/01/01-01-PLAN.md",
                expected_hash="aaa111bbb222",
                actual_hash="ccc333ddd444",
            )
        ]
        report.issues = [
            ReconcileIssue(
                plan_id="plan-drift",
                issue_type="drift",
                description="Source plan changed",
            )
        ]
        manifest = Manifest(project_root="/tmp", plans=[])
        md = generate_status_md(report, manifest)
        self.assertIn("## Drift Warnings", md)
        self.assertIn("plan-drift", md)
        self.assertIn("aaa111bbb222", md)
        self.assertIn("ccc333ddd444", md)

    def test_generate_status_md_blocked_section(self) -> None:
        from gsd_bridge.reconcile import generate_status_md
        from gsd_bridge.schemas import PlanState, ReconcileReport

        blocked_state = PlanState(
            plan_id="plan-b",
            source_plan_path=".planning/a.md",
            source_plan_hash="abc",
            status="blocked",
            blocked_reason="Missing API key",
            blocked_severity="critical",
            total_tasks=1,
        )
        report = ReconcileReport()
        report.plan_states = [blocked_state]
        report.summary = {"total": 1, "blocked": 1}
        manifest = Manifest(project_root="/tmp", plans=[])
        md = generate_status_md(report, manifest)
        self.assertIn("## Blocked Plans", md)
        self.assertIn("Missing API key", md)
        self.assertIn("CRITICAL", md)
        self.assertIn("!!!!", md)

    def test_generate_status_md_verification_detail_section(self) -> None:
        from gsd_bridge.reconcile import generate_status_md
        from gsd_bridge.schemas import PlanState, ReconcileReport

        state = PlanState(
            plan_id="plan-v",
            source_plan_path=".planning/a.md",
            source_plan_hash="abc",
            status="executing",
            total_tasks=2,
            completed_tasks=[1],
            verification={
                "quick": [{"command": "npx tsc", "exit_code": 0, "ran_at": ""}],
                "full": [{"command": "npm test", "exit_code": 1, "ran_at": ""}],
            },
        )
        report = ReconcileReport()
        report.plan_states = [state]
        report.summary = {"total": 1, "executing": 1}
        manifest = Manifest(project_root="/tmp", plans=[])
        md = generate_status_md(report, manifest)
        self.assertIn("## Verification Detail", md)
        self.assertIn("PASS", md)
        self.assertIn("FAIL", md)

    def test_reconcile_reports_missing_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(project_root=str(root), plans=[_entry("plan-a")])
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)
            # Intentionally do NOT create a state file for plan-a

            report = reconcile(manifest_path, root)
            issue_types = [i.issue_type for i in report.issues]
            self.assertIn("missing_state", issue_types)

    def test_resume_execution_active_lease_handoff(self) -> None:
        """Non-expired lock with a different run_id triggers lease_handoff recovery note."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "blocked"
            # Active (non-expired) lock from a different run
            state.lock = {
                "run_id": "old-run",
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
            write_state(state_path, state)

            result = resume_execution(state_path, {"run_id": "new-run"})
            self.assertEqual(result.lock["run_id"], "new-run")
            # Should have a lease_handoff recovery note
            handoff_notes = [
                n for n in result.recovery_notes if n.get("event") == "lease_handoff"
            ]
            self.assertEqual(len(handoff_notes), 1)
            self.assertEqual(handoff_notes[0]["previous_run_id"], "old-run")
            self.assertEqual(handoff_notes[0]["new_run_id"], "new-run")

    def test_resume_execution_blocks_on_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / ".planning" / "a.md"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("new-content", encoding="utf-8")

            state_path = root / "docs" / "plans" / "_state" / "plan-a.json"
            state = init_state(
                "plan-a",
                ".planning/a.md",
                content_hash("old-content"),
                total_tasks=1,
            )
            state.status = "blocked"
            write_state(state_path, state)

            with self.assertRaisesRegex(ValueError, "drift"):
                resume_execution(state_path, {"run_id": "run-2"})

    def test_reconcile_reports_lock_recovery_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(project_root=str(root), plans=[_entry("plan-a")])
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            state.status = "executing"
            state.last_run_at = datetime.now(timezone.utc).isoformat()
            state.recovery_notes = [
                {
                    "event": "lease_expired_takeover",
                    "taken_over_at": "2025-01-01T00:00:00+00:00",
                    "previous_run_id": "old-run",
                }
            ]
            write_state(state_dir / "plan-a.json", state)

            report = reconcile(manifest_path, root)
            issue_types = [i.issue_type for i in report.issues]
            self.assertIn("lock_recovery", issue_types)


    # ---------------------------------------------------------------
    # New top-level commands: blocked, execute, resume, unlock, validate --pending
    # ---------------------------------------------------------------

    def test_cmd_blocked_shows_blocked_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            blockers_dir = plans_dir / "_blockers"
            state_dir.mkdir(parents=True)
            blockers_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("plan-blocked", status="blocked"),
                    _entry("plan-pending", wave=1, plan_number=2, status="pending"),
                ],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s1 = init_state("plan-blocked", ".planning/a.md", "abc", total_tasks=1)
            s1.status = "blocked"
            s1.blocked_reason = "Missing API key"
            s1.blocked_severity = "critical"
            write_state(state_dir / "plan-blocked.json", s1)

            s2 = init_state("plan-pending", ".planning/b.md", "def", total_tasks=1)
            write_state(state_dir / "plan-pending.json", s2)

            (blockers_dir / "plan-blocked.md").write_text(
                "# Blocked\nMissing API key", encoding="utf-8"
            )

            rc = cli.main(["blocked", str(plans_dir)])
            self.assertEqual(rc, 0)

    def test_cmd_blocked_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[_entry("plan-b", status="blocked")],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s = init_state("plan-b", ".planning/a.md", "abc", total_tasks=1)
            s.status = "blocked"
            s.blocked_reason = "Need info"
            s.blocked_severity = "high"
            write_state(state_dir / "plan-b.json", s)

            output = io.StringIO()
            with redirect_stdout(output):
                rc = cli.main(["blocked", str(plans_dir), "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["plan_id"], "plan-b")
            self.assertEqual(payload[0]["severity"], "high")

    def test_cmd_blocked_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[_entry("plan-a", status="pending")],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_dir / "plan-a.json", s)

            rc = cli.main(["blocked", str(plans_dir)])
            self.assertEqual(rc, 0)

    def test_cmd_execute_starts_next_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[_entry("plan-a")],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s = init_state("plan-a", ".planning/a.md", "abc", total_tasks=2)
            write_state(state_dir / "plan-a.json", s)

            rc = cli.main(["execute", str(plans_dir)])
            self.assertEqual(rc, 0)

            updated = read_state(state_dir / "plan-a.json")
            assert updated is not None
            self.assertEqual(updated.status, "executing")

    def test_cmd_execute_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[_entry("plan-a")],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s = init_state("plan-a", ".planning/a.md", "abc", total_tasks=1)
            write_state(state_dir / "plan-a.json", s)

            rc = cli.main(["execute", str(plans_dir), "--dry-run"])
            self.assertEqual(rc, 0)

            # State should still be pending
            updated = read_state(state_dir / "plan-a.json")
            assert updated is not None
            self.assertEqual(updated.status, "pending")

    def test_cmd_execute_wave_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("wave1-a", wave=1, plan_number=1),
                    _entry("wave2-a", wave=2, plan_number=1),
                ],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s1 = init_state("wave1-a", ".planning/a.md", "a", total_tasks=1)
            s1.status = "verified"
            write_state(state_dir / "wave1-a.json", s1)

            s2 = init_state("wave2-a", ".planning/b.md", "b", total_tasks=1)
            write_state(state_dir / "wave2-a.json", s2)

            rc = cli.main(["execute", str(plans_dir), "--wave", "2"])
            self.assertEqual(rc, 0)

            updated = read_state(state_dir / "wave2-a.json")
            assert updated is not None
            self.assertEqual(updated.status, "executing")

    def test_cmd_execute_until_blocked_halts_when_blocker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("plan-pending", wave=1, plan_number=1),
                    _entry("plan-blocked", wave=1, plan_number=2, status="blocked"),
                ],
            )
            manifest.compute_summary()
            write_manifest(manifest, plans_dir / "_manifest.json")

            s1 = init_state("plan-pending", ".planning/a.md", "a", total_tasks=1)
            write_state(state_dir / "plan-pending.json", s1)

            s2 = init_state("plan-blocked", ".planning/b.md", "b", total_tasks=1)
            s2.status = "blocked"
            s2.blocked_reason = "waiting for API key"
            write_state(state_dir / "plan-blocked.json", s2)

            rc = cli.main(["execute", str(plans_dir), "--until", "blocked", "--max-plans", "5"])
            self.assertEqual(rc, 0)

            pending = read_state(state_dir / "plan-pending.json")
            assert pending is not None
            self.assertEqual(pending.status, "pending")

    def test_cmd_validate_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01"
            planning.mkdir(parents=True)

            plan_content = (
                "---\nphase: 01-foundation\nplan: 1\nwave: 1\n---\n"
                "<objective>Build foundation</objective>\n"
                "<tasks>\n"
                '<task type="auto"><name>Setup</name><action>Do it</action>'
                "<verify>Check</verify><done>Done</done></task>\n"
                "</tasks>\n"
                "<verification>Run tests</verification>\n"
                "<success_criteria>All pass</success_criteria>\n"
            )
            (planning / "01-01-PLAN.md").write_text(plan_content, encoding="utf-8")
            # Create a summary so this plan is NOT pending
            (planning / "01-01-SUMMARY.md").write_text("Done", encoding="utf-8")
            # Create another plan that IS pending
            (planning / "01-02-PLAN.md").write_text(plan_content, encoding="utf-8")

            import os
            old_cwd = os.getcwd()
            try:
                os.chdir(root)
                rc = cli.main(["validate", "--pending"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(rc, 0)

    def test_cmd_unlock_releases_stuck_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            s = init_state("plan-stuck", ".planning/a.md", "abc", total_tasks=1)
            s.status = "executing"
            s.lock = {
                "run_id": "dead-run",
                "acquired_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            }
            write_state(state_dir / "plan-stuck.json", s)

            rc = cli.main(["unlock", "plan-stuck", str(plans_dir), "--force", "--yes"])
            self.assertEqual(rc, 0)

            updated = read_state(state_dir / "plan-stuck.json")
            assert updated is not None
            self.assertEqual(updated.status, "executing")

    def test_cmd_resume_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            s = init_state("plan-blocked", ".planning/a.md", "abc", total_tasks=1)
            s.status = "blocked"
            s.blocked_reason = "waiting"
            write_state(state_dir / "plan-blocked.json", s)

            rc = cli.main(["resume", "plan-blocked", str(plans_dir), "--yes"])
            self.assertEqual(rc, 0)

            updated = read_state(state_dir / "plan-blocked.json")
            assert updated is not None
            self.assertEqual(updated.status, "executing")

    def test_cmd_resume_requires_yes_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            s = init_state("plan-blocked", ".planning/a.md", "abc", total_tasks=1)
            s.status = "blocked"
            write_state(state_dir / "plan-blocked.json", s)

            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["resume", "plan-blocked", str(plans_dir)])
            self.assertEqual(rc, 1)
            self.assertIn("--yes", err.getvalue())

    def test_get_eligible_plans_filters(self) -> None:
        from gsd_bridge.codex_adapter import get_eligible_plans

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("w1-a", wave=1, plan_number=1),
                    _entry("w1-b", wave=1, plan_number=2),
                    _entry("w2-a", wave=2, plan_number=1),
                ],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            for pid in ["w1-a", "w1-b", "w2-a"]:
                s = init_state(pid, f".planning/{pid}.md", pid, total_tasks=1)
                write_state(state_dir / f"{pid}.json", s)

            # All wave 1 plans eligible (wave 2 blocked by wave ordering)
            all_eligible = get_eligible_plans(manifest_path)
            self.assertEqual(len(all_eligible), 2)

            # Filter by wave
            wave1 = get_eligible_plans(manifest_path, wave=1)
            self.assertEqual(len(wave1), 2)
            wave2 = get_eligible_plans(manifest_path, wave=2)
            self.assertEqual(len(wave2), 0)

            # Filter by plan_id
            specific = get_eligible_plans(manifest_path, plan_id="w1-b")
            self.assertEqual(len(specific), 1)
            self.assertEqual(specific[0].plan_id, "w1-b")

    def test_get_eligible_plans_rejects_ambiguous_dependency_tokens(self) -> None:
        from gsd_bridge.codex_adapter import get_eligible_plans

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            manifest = Manifest(
                project_root=str(root),
                plans=[
                    _entry("a", wave=1, plan_number=1, status="verified"),
                    _entry("b", wave=1, plan_number=1, status="verified"),
                    _entry("c", wave=1, plan_number=2, depends_on=["1"]),
                ],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            s1 = init_state("a", ".planning/a.md", "a", total_tasks=1)
            s1.status = "verified"
            write_state(state_dir / "a.json", s1)

            s2 = init_state("b", ".planning/b.md", "b", total_tasks=1)
            s2.status = "verified"
            write_state(state_dir / "b.json", s2)

            s3 = init_state("c", ".planning/c.md", "c", total_tasks=1)
            write_state(state_dir / "c.json", s3)

            with self.assertRaisesRegex(ValueError, "Ambiguous dependency"):
                get_eligible_plans(manifest_path)


if __name__ == "__main__":
    unittest.main()

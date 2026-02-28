from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from gsd_bridge import cli
from gsd_bridge.converter import convert_to_superpowers
from gsd_bridge.codex_adapter import (
    DEFAULT_ROLLBACK_ALLOWLIST,
    _enforce_rollback_allowlist,
    _rollback_allowlist,
    check_drift,
    rollback_execution,
    update_state,
)
from gsd_bridge.manifest import generate_manifest, write_manifest
from gsd_bridge.parser import _parse_rollback_contract, parse_gsd_plan
from gsd_bridge.plan_id import content_hash, generate_plan_id
from gsd_bridge.schemas import CURRENT_SCHEMA_VERSION, Manifest, ManifestEntry
from gsd_bridge.state import init_state, read_state, state_lock, write_state


def _sample_plan_text() -> str:
    return """---
phase: 01-foundation
plan: 1
wave: 1
must_haves:
  truths:
    - It works
---
<objective>
Ship feature
</objective>
<context>
Context here
</context>
<tasks>
<task type="auto">
<name>Task 1: Add tests</name>
<files>tests/test_x.py</files>
<action>1. Write test</action>
<verify>python -m unittest -v</verify>
<done>Tests pass</done>
</task>
</tasks>
<verification>
1. python -m unittest -v
</verification>
<success_criteria>
Done
</success_criteria>
<execution_contract>
<inputs>None</inputs>
<outputs>docs/plans</outputs>
<side_effects>none</side_effects>
<rollback>{"argv": ["echo", "rollback"]}</rollback>
</execution_contract>
"""


def _write_sample_plan(plan_path: Path) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(_sample_plan_text(), encoding="utf-8")


class ContractSurfaceTests(unittest.TestCase):
    def test_parser_to_model_extracts_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / ".planning" / "phases" / "01-foundation" / "01-01-PLAN.md"
            _write_sample_plan(plan_path)

            parsed = parse_gsd_plan(plan_path)
            self.assertEqual(parsed["frontmatter"]["phase"], "01-foundation")
            self.assertEqual(len(parsed["tasks"]), 1)
            contract = parsed["execution_contract"]
            assert contract is not None
            self.assertIn("rollback", contract)

    def test_plan_id_stability_same_content_same_id(self) -> None:
        source_path = Path(".planning/phases/01-foundation/01-01-PLAN.md")
        content = _sample_plan_text()
        first = generate_plan_id(source_path, content)
        second = generate_plan_id(source_path, content)
        self.assertEqual(first, second)

    def test_manifest_generation_creates_and_orders_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_output = root / "custom" / "plans"

            plan_a_path = root / ".planning" / "phases" / "02-two" / "02-02-PLAN.md"
            plan_b_path = root / ".planning" / "phases" / "01-one" / "01-01-PLAN.md"
            _write_sample_plan(plan_a_path)
            _write_sample_plan(plan_b_path)

            parsed_a = parse_gsd_plan(plan_a_path)
            parsed_a["frontmatter"]["phase"] = "02-two"
            parsed_a["frontmatter"]["plan"] = 2
            parsed_a["frontmatter"]["wave"] = 2

            parsed_b = parse_gsd_plan(plan_b_path)
            parsed_b["frontmatter"]["phase"] = "01-one"
            parsed_b["frontmatter"]["plan"] = 1
            parsed_b["frontmatter"]["wave"] = 1

            manifest = generate_manifest([parsed_a, parsed_b], plans_output, root)

            self.assertEqual(len(manifest.plans), 2)
            self.assertEqual(manifest.plans[0].wave, 1)
            self.assertEqual(manifest.summary["total"], 2)
            state_file = plans_output / "_state" / f"{manifest.plans[0].plan_id}.json"
            self.assertTrue(state_file.exists())

    def test_manifest_default_version_matches_current_schema(self) -> None:
        manifest = Manifest(project_root="/tmp", plans=[])
        self.assertEqual(manifest.version, CURRENT_SCHEMA_VERSION)

    def test_converter_execution_notes_honor_custom_plans_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / ".planning" / "phases" / "01-foundation" / "01-01-PLAN.md"
            _write_sample_plan(plan_path)

            parsed = parse_gsd_plan(plan_path)
            plan_id = generate_plan_id(plan_path, parsed["raw_content"])
            out = convert_to_superpowers(parsed, plan_id, plans_dir="custom/plans")

            self.assertIn("`custom/plans/_manifest.json`", out)
            self.assertIn(f"`custom/plans/_state/{plan_id}.json`", out)

    def test_state_lock_recovers_stale_lease_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state" / "x.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = state_path.with_suffix(".json.lock")
            lock_path.write_text("stale", encoding="utf-8")

            with state_lock(
                state_path,
                timeout_seconds=0.1,
                retry_interval_seconds=0.01,
                lease_seconds=0.01,
            ):
                self.assertTrue(lock_path.exists())

    def test_update_state_rejects_invalid_field_and_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "custom" / "plans" / "_state" / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "abc", total_tasks=2)
            write_state(state_path, state)

            with self.assertRaises(ValueError):
                update_state(state_path, unknown_field="x")

            with self.assertRaises(ValueError):
                update_state(state_path, status="verified")

    def test_rollback_rejects_raw_shell_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "custom" / "plans"
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
                        plan_path=f"custom/plans/{plan_id}.md",
                        state_path=f"custom/plans/_state/{plan_id}.json",
                        source_path=".planning/phases/01/01-01-PLAN.md",
                        source_hash="hash-a",
                        status="failed",
                        execution_contract={"rollback": "echo rollback"},
                    )
                ],
            )
            manifest.compute_summary()
            manifest_path = plans_dir / "_manifest.json"
            write_manifest(manifest, manifest_path)

            state = init_state(plan_id, ".planning/a.md", "abc", total_tasks=1)
            state.status = "failed"
            state_path = state_dir / f"{plan_id}.json"
            write_state(state_path, state)

            with self.assertRaises(ValueError):
                rollback_execution(manifest_path, state_path)

    def test_rollback_executes_structured_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "custom" / "plans"
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
                        plan_path=f"custom/plans/{plan_id}.md",
                        state_path=f"custom/plans/_state/{plan_id}.json",
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
            state_path = state_dir / f"{plan_id}.json"
            write_state(state_path, state)

            result = rollback_execution(manifest_path, state_path)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["argv"][:2], ["echo", "rollback"])


    # ---------------------------------------------------------------
    # Step 8: _rollback_allowlist env var override
    # ---------------------------------------------------------------

    def test_rollback_allowlist_env_override(self) -> None:
        os.environ["GSD_BRIDGE_ROLLBACK_ALLOWLIST"] = "make,custom-tool"
        try:
            allowlist = _rollback_allowlist()
            self.assertIn("make", allowlist)
            self.assertIn("custom-tool", allowlist)
            self.assertNotIn("git", allowlist)
        finally:
            del os.environ["GSD_BRIDGE_ROLLBACK_ALLOWLIST"]

    def test_rollback_allowlist_default_when_env_not_set(self) -> None:
        os.environ.pop("GSD_BRIDGE_ROLLBACK_ALLOWLIST", None)
        allowlist = _rollback_allowlist()
        for cmd in DEFAULT_ROLLBACK_ALLOWLIST:
            self.assertIn(cmd, allowlist)

    def test_enforce_allowlist_rejects_unlisted_command(self) -> None:
        os.environ.pop("GSD_BRIDGE_ROLLBACK_ALLOWLIST", None)
        with self.assertRaises(ValueError):
            _enforce_rollback_allowlist("rm")

    # ---------------------------------------------------------------
    # Env var override tests for _resolve_runtime_paths
    # ---------------------------------------------------------------

    def test_plans_dir_env_override(self) -> None:
        from gsd_bridge.codex_adapter import _resolve_runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            custom_plans = Path(tmp) / "my_plans"
            custom_plans.mkdir()
            state_path = Path(tmp) / "_state" / "plan-a.json"
            os.environ["GSD_BRIDGE_PLANS_DIR"] = str(custom_plans)
            try:
                plans_dir, _project_root = _resolve_runtime_paths(state_path=state_path)
                self.assertEqual(plans_dir, custom_plans.resolve())
            finally:
                del os.environ["GSD_BRIDGE_PLANS_DIR"]

    def test_project_root_env_override(self) -> None:
        from gsd_bridge.codex_adapter import _resolve_runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            custom_root = Path(tmp) / "my_project"
            custom_root.mkdir()
            state_path = Path(tmp) / "_state" / "plan-a.json"
            os.environ["GSD_BRIDGE_PROJECT_ROOT"] = str(custom_root)
            try:
                _plans_dir, project_root = _resolve_runtime_paths(state_path=state_path)
                self.assertEqual(project_root, custom_root.resolve())
            finally:
                del os.environ["GSD_BRIDGE_PROJECT_ROOT"]


# ===================================================================
# Step 5: check_drift tests
# ===================================================================

class DriftTests(unittest.TestCase):
    def test_check_drift_missing_state_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(check_drift(root / "missing.json", root / "plan.md"))

    def test_check_drift_missing_source_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "docs" / "plans" / "_state"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "somehash", total_tasks=1)
            write_state(state_path, state)
            self.assertTrue(check_drift(state_path, root / "nonexistent.md"))

    def test_check_drift_hashes_match_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_content = "# My Plan\nDo stuff."
            plan_path = root / "plan.md"
            plan_path.write_text(plan_content, encoding="utf-8")

            state_dir = root / "docs" / "plans" / "_state"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", content_hash(plan_content), total_tasks=1)
            write_state(state_path, state)
            self.assertFalse(check_drift(state_path, plan_path))

    def test_check_drift_hashes_differ_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.md"
            plan_path.write_text("changed content", encoding="utf-8")

            state_dir = root / "docs" / "plans" / "_state"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "plan-a.json"
            state = init_state("plan-a", ".planning/a.md", "old-hash", total_tasks=1)
            write_state(state_path, state)
            self.assertTrue(check_drift(state_path, plan_path))


# ===================================================================
# Step 6: YAML rollback parsing tests
# ===================================================================

class RollbackParserTests(unittest.TestCase):
    def test_parse_rollback_json_succeeds(self) -> None:
        result = _parse_rollback_contract('{"argv": ["git", "checkout", "."]}')
        self.assertIsInstance(result, dict)
        self.assertEqual(result["argv"], ["git", "checkout", "."])

    def test_parse_rollback_yaml_fallback(self) -> None:
        yaml_text = "argv:\n  - git\n  - checkout\n  - ."
        result = _parse_rollback_contract(yaml_text)
        self.assertIsInstance(result, dict)
        self.assertIn("argv", result)
        self.assertEqual(result["argv"], ["git", "checkout", "."])

    def test_parse_rollback_both_fail_returns_raw_string(self) -> None:
        text = "just run git checkout ."
        result = _parse_rollback_contract(text)
        self.assertEqual(result, text)

    def test_parse_rollback_empty_returns_none(self) -> None:
        self.assertIsNone(_parse_rollback_contract(""))
        self.assertIsNone(_parse_rollback_contract("   "))


# ===================================================================
# CLI integration tests
# ===================================================================

class CliIntegrationTests(unittest.TestCase):
    def test_export_status_json_archive_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            plan_path = planning / "01-01-PLAN.md"
            _write_sample_plan(plan_path)

            plans_dir = root / "custom" / "plans"
            rc_export = cli.main(["export", str(planning), "--output-dir", str(plans_dir)])
            self.assertEqual(rc_export, 0)

            status_out = io.StringIO()
            with redirect_stdout(status_out):
                rc_status = cli.main(["status", str(plans_dir), "--json"])
            self.assertEqual(rc_status, 0)
            payload = json.loads(status_out.getvalue())
            self.assertEqual(payload["summary"]["total"], 1)

            manifest = Manifest.from_json((plans_dir / "_manifest.json").read_text(encoding="utf-8"))
            plan_id = manifest.plans[0].plan_id

            # Simulate completion so archive passes status checks.
            state_path = plans_dir / "_state" / f"{plan_id}.json"
            state = read_state(state_path)
            assert state is not None
            state.status = "verified"
            write_state(state_path, state)
            self.assertEqual(cli.main(["refresh", str(plans_dir)]), 0)

            rc_archive = cli.main(["archive", str(plans_dir), plan_id, "--dry-run"])
            self.assertEqual(rc_archive, 0)
            self.assertFalse((plans_dir / "_archive" / plan_id / f"{plan_id}.json").exists())

    def test_export_prints_valid_next_command_for_custom_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "custom" / "plans"

            err = io.StringIO()
            with redirect_stderr(err):
                rc_export = cli.main(["export", str(planning), "--output-dir", str(plans_dir)])
            self.assertEqual(rc_export, 0)
            self.assertIn(f"gsd-bridge execute {plans_dir}", err.getvalue())

    def test_status_returns_error_for_invalid_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"
            self.assertEqual(cli.main(["export", str(planning), "-o", str(plans_dir)]), 0)

            manifest = Manifest.from_json((plans_dir / "_manifest.json").read_text(encoding="utf-8"))
            plan_id = manifest.plans[0].plan_id
            (plans_dir / "_state" / f"{plan_id}.json").write_text("{bad", encoding="utf-8")

            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main(["status", str(plans_dir)])
            self.assertEqual(rc, 1)
            self.assertIn("Invalid state JSON", err.getvalue())

    # ---------------------------------------------------------------
    # Step 4: cmd_validate tests
    # ---------------------------------------------------------------

    def test_cmd_validate_nonexistent_path_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.cmd_validate(
                argparse.Namespace(plan_path=Path(tmp) / "does_not_exist.md")
            )
            self.assertEqual(rc, 1)

    def test_cmd_validate_valid_plan_returns_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / ".planning" / "phases" / "01-foundation" / "01-01-PLAN.md"
            _write_sample_plan(plan_path)
            rc = cli.cmd_validate(argparse.Namespace(plan_path=plan_path))
            self.assertEqual(rc, 0)

    def test_cmd_validate_invalid_plan_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / ".planning" / "phases" / "01-foundation" / "01-01-PLAN.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            # Write a plan missing success_criteria
            plan_path.write_text(
                "---\nphase: 01-foundation\nplan: 1\n---\n"
                "<objective>X</objective>\n"
                "<tasks><task type=\"auto\"><name>T</name><files></files>"
                "<action>A</action><verify>V</verify><done>D</done></task></tasks>\n"
                "<verification>V</verification>\n",
                encoding="utf-8",
            )
            rc = cli.cmd_validate(argparse.Namespace(plan_path=plan_path))
            self.assertEqual(rc, 1)

    # ---------------------------------------------------------------
    # Step 9: cmd_export --dry-run + cmd_archive --delete / --force
    # ---------------------------------------------------------------

    def test_cmd_export_dry_run_no_files_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"

            output = io.StringIO()
            with redirect_stdout(output):
                rc = cli.main(["export", str(planning), "-o", str(plans_dir), "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("====", output.getvalue())
            self.assertFalse((plans_dir / "_manifest.json").exists())

    def test_cmd_archive_delete_removes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"
            cli.main(["export", str(planning), "-o", str(plans_dir)])

            manifest = Manifest.from_json(
                (plans_dir / "_manifest.json").read_text(encoding="utf-8")
            )
            plan_id = manifest.plans[0].plan_id

            state_path = plans_dir / "_state" / f"{plan_id}.json"
            state = read_state(state_path)
            assert state is not None
            state.status = "verified"
            write_state(state_path, state)
            cli.main(["refresh", str(plans_dir)])

            rc = cli.main(["archive", str(plans_dir), plan_id, "--delete"])
            self.assertEqual(rc, 0)
            self.assertFalse((plans_dir / f"{plan_id}.md").exists())
            self.assertFalse((plans_dir / "_archive" / plan_id / f"{plan_id}.md").exists())

    def test_cmd_export_invalid_plans_reported(self) -> None:
        """Invalid plans (missing success_criteria) are counted and reported."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            plan_path = planning / "01-01-PLAN.md"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            # Plan missing success_criteria → invalid
            plan_path.write_text(
                "---\nphase: 01-foundation\nplan: 1\n---\n"
                "<objective>X</objective>\n"
                "<tasks><task type=\"auto\"><name>T</name><files></files>"
                "<action>A</action><verify>V</verify><done>D</done></task></tasks>\n"
                "<verification>V</verification>\n",
                encoding="utf-8",
            )
            plans_dir = root / "docs" / "plans"
            rc = cli.main(["export", str(planning), "-o", str(plans_dir)])
            self.assertEqual(rc, 1)  # invalid plans → exit code 1

    def test_cmd_status_tabular_output(self) -> None:
        """Non-JSON status output produces a human-readable table."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"
            cli.main(["export", str(planning), "-o", str(plans_dir)])

            output = io.StringIO()
            with redirect_stdout(output):
                rc = cli.main(["status", str(plans_dir)])
            self.assertEqual(rc, 0)
            table = output.getvalue()
            self.assertIn("Plan ID", table)
            self.assertIn("Status", table)
            self.assertIn("pending", table)

    def test_cmd_migrate_error_handling(self) -> None:
        """cmd_migrate returns 1 when a state file contains invalid JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans_dir = root / "docs" / "plans"
            state_dir = plans_dir / "_state"
            state_dir.mkdir(parents=True)

            (state_dir / "bad.json").write_text("{not valid json}", encoding="utf-8")

            rc = cli.cmd_migrate(argparse.Namespace(plans_dir=plans_dir))
            self.assertEqual(rc, 1)

    def test_cmd_archive_force_overrides_status_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"
            cli.main(["export", str(planning), "-o", str(plans_dir)])

            manifest = Manifest.from_json(
                (plans_dir / "_manifest.json").read_text(encoding="utf-8")
            )
            plan_id = manifest.plans[0].plan_id

            # Status is "pending" (not verified) — archive without --force should fail
            rc1 = cli.main(["archive", str(plans_dir), plan_id])
            self.assertEqual(rc1, 1)

            # With --force it should succeed
            rc2 = cli.main(["archive", str(plans_dir), plan_id, "--force"])
            self.assertEqual(rc2, 0)


# ===================================================================
# gsd_to_superpowers.py backward-compat wrapper tests
# ===================================================================

class V1WrapperTests(unittest.TestCase):
    def test_v1_wrapper_prepends_export_subcommand(self) -> None:
        """gsd_to_superpowers.main() delegates to bridge export."""
        import sys
        import gsd_to_superpowers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"

            # Simulate v1 CLI args: gsd_to_superpowers.py <path> -o <dir>
            original_argv = sys.argv
            sys.argv = ["gsd_to_superpowers.py", str(planning), "-o", str(plans_dir)]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    gsd_to_superpowers.main()
                self.assertEqual(ctx.exception.code, 0)
            finally:
                sys.argv = original_argv

            # Verify export actually ran
            self.assertTrue((plans_dir / "_manifest.json").exists())

    def test_v1_wrapper_strips_state_flag(self) -> None:
        """--state flag is silently stripped; export still succeeds."""
        import sys
        import gsd_to_superpowers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planning = root / ".planning" / "phases" / "01-foundation"
            _write_sample_plan(planning / "01-01-PLAN.md")
            plans_dir = root / "docs" / "plans"

            original_argv = sys.argv
            sys.argv = [
                "gsd_to_superpowers.py", str(planning),
                "--state", "-o", str(plans_dir),
            ]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    gsd_to_superpowers.main()
                self.assertEqual(ctx.exception.code, 0)
            finally:
                sys.argv = original_argv

    def test_v1_wrapper_no_args_shows_usage(self) -> None:
        """No args prints usage and exits with code 1."""
        import sys
        import gsd_to_superpowers

        original_argv = sys.argv
        sys.argv = ["gsd_to_superpowers.py"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                gsd_to_superpowers.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = original_argv


class ToolingDefaultsTests(unittest.TestCase):
    def test_makefile_defaults_to_python3(self) -> None:
        makefile = Path(__file__).resolve().parents[1] / "Makefile"
        text = makefile.read_text(encoding="utf-8")
        self.assertIn("PYTHON ?= python3", text)


if __name__ == "__main__":
    unittest.main()

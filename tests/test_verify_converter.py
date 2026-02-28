from __future__ import annotations

import unittest
from typing import Any

from gsd_bridge.converter import (
    _compute_batches,
    _strip_task_prefix,
    _task_batch_number,
    convert_to_superpowers,
)
from gsd_bridge.verify import (
    _classify_line,
    classify_task_verify,
    parse_verify_tiers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_parsed(
    *,
    tasks: list[dict[str, Any]] | None = None,
    verification: str = "",
    success_criteria: str = "Done",
    execution_contract: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Minimal parsed plan dict for converter tests."""
    if tasks is None:
        tasks = [
            {
                "name": "Task 1: Setup",
                "files": "",
                "action": "Do it",
                "verify": "",
                "done": "",
                "type": "auto",
                "gate": None,
            }
        ]
    return {
        "source_path": ".planning/phases/01/01-01-PLAN.md",
        "raw_content": "raw plan content",
        "frontmatter": {
            "phase": "01-foundation",
            "plan": 1,
            "wave": 1,
            "files_modified": [],
            "must_haves": {"truths": []},
            "depends_on": depends_on or [],
            "batch_size": 3,
            "batching": None,
        },
        "objective": "Ship it",
        "tasks": tasks,
        "verification": verification,
        "success_criteria": success_criteria,
        "execution_contract": execution_contract,
    }


# ===================================================================
# verify.py tests
# ===================================================================

class VerifyTierTests(unittest.TestCase):

    # --- parse_verify_tiers ---

    def test_parse_verify_tiers_empty_input(self) -> None:
        self.assertEqual(parse_verify_tiers(""), {})
        self.assertEqual(parse_verify_tiers("   "), {})

    def test_parse_verify_tiers_numbered_items_split(self) -> None:
        text = "1. npx tsc --noEmit\n2. npm run dev"
        result = parse_verify_tiers(text)
        self.assertIn("quick", result)
        self.assertIn("smoke", result)
        self.assertEqual(len(result["quick"]), 1)
        self.assertEqual(len(result["smoke"]), 1)

    def test_parse_verify_tiers_newline_fallback(self) -> None:
        text = "npm run lint\ngrep -r 'foo' src/"
        result = parse_verify_tiers(text)
        self.assertIn("quick", result)
        self.assertEqual(len(result["quick"]), 2)

    def test_parse_verify_tiers_default_full_tier(self) -> None:
        result = parse_verify_tiers("npm test")
        self.assertIn("full", result)
        self.assertNotIn("quick", result)
        self.assertNotIn("smoke", result)

    def test_parse_verify_tiers_smoke_patterns(self) -> None:
        result = parse_verify_tiers("curl http://localhost:3000/api/health")
        self.assertIn("smoke", result)

        result2 = parse_verify_tiers("Run Lighthouse on the homepage")
        self.assertIn("smoke", result2)

    def test_parse_verify_tiers_mixed(self) -> None:
        text = "1. npx tsc --noEmit\n2. npm test\n3. curl http://localhost:3000"
        result = parse_verify_tiers(text)
        self.assertIn("quick", result)
        self.assertIn("full", result)
        self.assertIn("smoke", result)

    # --- classify_task_verify ---

    def test_classify_task_verify_single_line(self) -> None:
        result = classify_task_verify("npx eslint .")
        self.assertIn("quick", result)
        self.assertEqual(len(result["quick"]), 1)

    def test_classify_task_verify_empty(self) -> None:
        self.assertEqual(classify_task_verify(""), {})
        self.assertEqual(classify_task_verify("  \n  "), {})

    def test_classify_task_verify_multiline_mixed(self) -> None:
        text = "npx eslint .\ncurl http://localhost:3000"
        result = classify_task_verify(text)
        self.assertIn("quick", result)
        self.assertIn("smoke", result)

    # --- _classify_line ---

    def test_classify_line_quick_patterns(self) -> None:
        quick_examples = [
            "grep foo src/",
            "npx tsc --noEmit",
            "npm run lint",
            "npx eslint src/",
            "npx prettier --check .",
            "rg 'export' src/",
            "Verify that Button imports theme",
            "Verify file exists at src/main.ts",
            "Check that component imports useEffect",
        ]
        for example in quick_examples:
            self.assertEqual(
                _classify_line(example), "quick",
                f"Expected 'quick' for: {example!r}",
            )

    def test_classify_line_smoke_patterns(self) -> None:
        smoke_examples = [
            "http://localhost:3000",
            "Run Lighthouse audit",
            "curl the endpoint",
            "visually inspect the layout",
            "open the browser",
            "manually confirm behavior",
            "npm run dev",
            "next start",
            "Check Chrome DevTools",
        ]
        for example in smoke_examples:
            self.assertEqual(
                _classify_line(example), "smoke",
                f"Expected 'smoke' for: {example!r}",
            )

    def test_classify_line_default_full(self) -> None:
        full_examples = [
            "npm test",
            "python -m unittest",
            "cargo build --release",
            "npx next build",
        ]
        for example in full_examples:
            self.assertEqual(
                _classify_line(example), "full",
                f"Expected 'full' for: {example!r}",
            )


# ===================================================================
# converter.py tests
# ===================================================================

class ConverterTests(unittest.TestCase):

    # --- _strip_task_prefix ---

    def test_strip_task_prefix_removes_prefix(self) -> None:
        self.assertEqual(_strip_task_prefix("Task 1: Add tests"), "Add tests")
        self.assertEqual(_strip_task_prefix("Task 12: Refactor"), "Refactor")

    def test_strip_task_prefix_no_op_when_absent(self) -> None:
        self.assertEqual(_strip_task_prefix("Add tests"), "Add tests")
        self.assertEqual(_strip_task_prefix(""), "")

    # --- _compute_batches ---

    def test_compute_batches_uniform(self) -> None:
        result = _compute_batches(5, 2, None)
        self.assertEqual(result, [[1, 2], [3, 4], [5]])

    def test_compute_batches_exact_fit(self) -> None:
        result = _compute_batches(4, 2, None)
        self.assertEqual(result, [[1, 2], [3, 4]])

    def test_compute_batches_custom_takes_priority(self) -> None:
        custom = [[1, 2, 3], [4]]
        result = _compute_batches(4, 99, custom)
        self.assertEqual(result, custom)

    # --- _task_batch_number ---

    def test_task_batch_number_finds_correct_batch(self) -> None:
        batches = [[1, 2], [3, 4]]
        self.assertEqual(_task_batch_number(1, batches), 1)
        self.assertEqual(_task_batch_number(2, batches), 1)
        self.assertEqual(_task_batch_number(3, batches), 2)
        self.assertEqual(_task_batch_number(4, batches), 2)

    def test_task_batch_number_fallback(self) -> None:
        self.assertEqual(_task_batch_number(99, [[1, 2]]), 1)

    # --- convert_to_superpowers ---

    def test_convert_contains_plan_id(self) -> None:
        out = convert_to_superpowers(_minimal_parsed(), "plan-abc123")
        self.assertIn("plan-abc123", out)

    def test_convert_contains_metadata_table(self) -> None:
        out = convert_to_superpowers(_minimal_parsed(), "plan-abc123")
        self.assertIn("| Phase |", out)
        self.assertIn("| Wave |", out)
        self.assertIn("| Total tasks |", out)

    def test_convert_contains_task_header(self) -> None:
        out = convert_to_superpowers(_minimal_parsed(), "plan-abc123")
        self.assertIn("### Task 1: Setup", out)

    def test_convert_batch_annotation_present(self) -> None:
        out = convert_to_superpowers(_minimal_parsed(), "plan-abc123")
        self.assertIn("Batch 1 of", out)

    def test_convert_execution_notes_present(self) -> None:
        out = convert_to_superpowers(_minimal_parsed(), "plan-abc123")
        self.assertIn("docs/plans/_manifest.json", out)
        self.assertIn("docs/plans/_state/plan-abc123.json", out)

    def test_convert_with_verification_tiers(self) -> None:
        parsed = _minimal_parsed(verification="1. npx tsc\n2. npm run dev")
        out = convert_to_superpowers(parsed, "plan-tiers")
        self.assertIn("Verify (Quick)", out)
        self.assertIn("Verify (Smoke)", out)

    def test_convert_with_execution_contract(self) -> None:
        contract = {
            "inputs": "Node 18+",
            "outputs": "Built dist/",
            "rollback": {"argv": ["git", "checkout", "."]},
        }
        parsed = _minimal_parsed(execution_contract=contract)
        out = convert_to_superpowers(parsed, "plan-contract")
        self.assertIn("Execution Contract", out)
        self.assertIn("Inputs", out)
        self.assertIn("Rollback", out)

    def test_convert_with_depends_on(self) -> None:
        parsed = _minimal_parsed(depends_on=["other-plan-id"])
        out = convert_to_superpowers(parsed, "plan-deps")
        self.assertIn("other-plan-id", out)

    # --- Gap: checkpoint task rendering ---

    def test_convert_checkpoint_task_with_gate(self) -> None:
        tasks = [
            {
                "name": "Task 1: Review",
                "files": "",
                "action": "Check outputs",
                "verify": "",
                "done": "Approved",
                "type": "checkpoint:human-verify",
                "gate": "blocking",
            }
        ]
        out = convert_to_superpowers(_minimal_parsed(tasks=tasks), "plan-cp")
        self.assertIn("[checkpoint:human-verify]", out)
        self.assertIn("(gate: blocking)", out)

    # --- Gap: files_modified and must_haves.truths ---

    def test_convert_with_files_modified(self) -> None:
        parsed = _minimal_parsed()
        parsed["frontmatter"]["files_modified"] = ["src/app.ts", "src/utils.ts"]
        out = convert_to_superpowers(parsed, "plan-files")
        self.assertIn("Files Involved", out)
        self.assertIn("`src/app.ts`", out)
        self.assertIn("`src/utils.ts`", out)

    def test_convert_with_success_criteria_truths(self) -> None:
        parsed = _minimal_parsed()
        parsed["frontmatter"]["must_haves"]["truths"] = [
            "All tests pass",
            "No lint errors",
        ]
        out = convert_to_superpowers(parsed, "plan-truths")
        self.assertIn("Success Criteria", out)
        self.assertIn("- [ ] All tests pass", out)
        self.assertIn("- [ ] No lint errors", out)

    # --- Gap: per-task verify tiers in output ---

    def test_convert_task_with_verify_tiers(self) -> None:
        tasks = [
            {
                "name": "Task 1: Build",
                "files": "src/main.ts",
                "action": "Build the app",
                "verify": "npx tsc --noEmit\ncurl http://localhost:3000",
                "done": "Builds clean",
                "type": "auto",
                "gate": None,
            }
        ]
        out = convert_to_superpowers(_minimal_parsed(tasks=tasks), "plan-tv")
        self.assertIn("Verify (Quick)", out)
        self.assertIn("Verify (Smoke)", out)

    # --- Gap: parse_verify_tiers with ) delimiter ---

    def test_parse_verify_tiers_paren_delimiter(self) -> None:
        text = "1) npx tsc --noEmit\n2) npm test"
        result = parse_verify_tiers(text)
        self.assertIn("quick", result)
        self.assertIn("full", result)


if __name__ == "__main__":
    unittest.main()

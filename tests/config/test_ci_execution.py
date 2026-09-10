"""Dynamic matrices and final status must correspond to the same selection."""

import copy
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import ci_execution as execution
    from crossforge_internal.identity import IdentityError
    BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
finally:
    sys.path.pop(0)


class CIExecutionTests(unittest.TestCase):
    def selection(self):
        return {"schema_version": 1, "kind": "crossforge-ci-source-plan", "mode": "incremental",
                "targets": {"python-cp39": ["python-cp39-dev"], "sdk": ["sdk-complete-dev"]}}

    def outputs(self):
        return execution.output_values(self.selection(), BUILD["STAGES"])

    def results(self):
        outputs = self.outputs()
        results = {"plan": {"result": "success", "outputs": outputs}}
        results.update({job: {"result": "success" if outputs[job] == "true" else "skipped"}
                        for job in execution.GROUPS})
        return results

    def test_row_only_matrix_does_not_schedule_other_rows_or_toolchains(self):
        value = self.outputs()
        self.assertEqual(json.loads(value["python-matrix"]), ["python-cp39"])
        self.assertEqual(value["toolchains"], "false")
        self.assertEqual(value["inputs"], "false")
        self.assertEqual(value["sdk"], "true")
        self.assertTrue(execution.check_results(self.results(), BUILD["STAGES"]))

    def test_final_gate_rejects_each_missing_cancelled_failed_or_unexpected_job(self):
        for job in execution.GROUPS:
            for result in ("failure", "cancelled", "skipped" if self.outputs()[job] == "true" else "success"):
                changed = self.results()
                changed[job]["result"] = result
                with self.subTest(job=job, result=result):
                    self.assertFalse(execution.check_results(changed, BUILD["STAGES"]))
            changed = self.results()
            del changed[job]
            self.assertFalse(execution.check_results(changed, BUILD["STAGES"]))
        changed = self.results()
        changed["unplanned"] = {"result": "success"}
        self.assertFalse(execution.check_results(changed, BUILD["STAGES"]))

    def test_final_gate_rejects_matrix_flags_or_selection_drift(self):
        for field in self.outputs():
            changed = self.results()
            del changed["plan"]["outputs"][field]
            self.assertFalse(execution.check_results(changed, BUILD["STAGES"]))
        changed = self.results()
        changed["plan"]["outputs"]["python-matrix"] = '["python-cp310"]'
        self.assertFalse(execution.check_results(changed, BUILD["STAGES"]))
        changed = self.results()
        changed["plan"]["outputs"]["python"] = "false"
        changed["python"]["result"] = "skipped"
        self.assertFalse(execution.check_results(changed, BUILD["STAGES"]))

    def test_full_fallback_and_none_retain_existing_profile_coverage(self):
        full = execution.prepare("", "full", BUILD["STAGES"])
        self.assertTrue(all(full[job] == "true" for job in execution.GROUPS))
        empty = execution.prepare("", "none", BUILD["STAGES"])
        self.assertTrue(all(empty[job] == "false" for job in execution.GROUPS))
        self.assertEqual(json.loads(empty["selection"])["targets"], {})
        partial = self.selection()
        partial["mode"] = "full"
        with self.assertRaises(IdentityError):
            execution.output_values(partial, BUILD["STAGES"])
        partial = self.selection()
        partial["targets"]["qt-inputs"] = BUILD["STAGES"]["qt-inputs"]
        with self.assertRaises(IdentityError):
            execution.output_values(partial, BUILD["STAGES"])

    def test_duplicate_json_and_optimization_cannot_disable_the_gate(self):
        with self.assertRaises(IdentityError):
            execution.prepare('{"schema_version":1,"schema_version":1}', "full", BUILD["STAGES"])
        bad = self.results()
        bad["python"]["result"] = "skipped"
        for results, status in ((self.results(), 0), (bad, 1)):
            completed = subprocess.run([sys.executable, str(ROOT / "scripts/ci-component-plan.py"), "check",
                json.dumps(results)], cwd=ROOT, env=dict(os.environ, PYTHONOPTIMIZE="2"), capture_output=True)
            self.assertEqual(completed.returncode, status, completed.stderr)

    def test_full_mode_cannot_drop_roots_inside_a_selected_stage(self):
        canonical = execution.prepare("", "full", BUILD["STAGES"])
        partial = json.loads(canonical["selection"])
        partial["targets"]["inputs"] = ["validate"]
        partial["targets"]["sdk"] = ["sdk-complete-dev"]
        # Producer selection is expanded, while a narrowed execution record is
        # rejected by the independently recomputed final gate.
        self.assertEqual(execution.prepare(json.dumps(partial), "full", BUILD["STAGES"]), canonical)
        with self.assertRaises(IdentityError):
            execution.output_values(partial, BUILD["STAGES"])

    def test_stage_target_subset_cannot_escape_its_canonical_group(self):
        graph = {"group": {"default": {"targets": ["all"]}, "all": {"targets": ["one", "two"]}},
                 "target": {name: {} for name in ("one", "two", "linked")}}
        function = BUILD["selected_graph"]
        with mock.patch.dict(function.__globals__, {"read_graph": mock.Mock(return_value=graph), "STAGES": {"sdk": ["all"]}}):
            self.assertEqual(function("sdk", ["two"]), graph)
            self.assertEqual(function.__globals__["read_graph"].call_args.args, (["two"],))
            for invalid in ([], ["linked"], ["other"], ["one", "one"], "one", [None], ["--push"]):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    function("sdk", invalid)

    def test_workflow_selected_jobs_accept_deliberate_skips_but_reject_failed_ancestors(self):
        workflow = (ROOT / ".github/workflows/verify-incremental.yml").read_text()
        self.assertNotIn("continue-on-error", workflow)
        self.assertNotIn("packages: write", workflow)
        for job in execution.GROUPS:
            block = re.split(r"\n  [A-Za-z0-9_-]+:\n", workflow.split("\n  " + job + ":\n", 1)[1], 1)[0]
            self.assertIn("always() && !cancelled()", block)
            self.assertIn("needs.plan.outputs." + job + " == 'true'", block)
            self.assertIn("!contains(needs.*.result, 'failure')", block)
            self.assertIn("!contains(needs.*.result, 'cancelled')", block)
        sdk = workflow.split("\n  sdk:\n", 1)[1]
        self.assertIn("needs: [plan, inputs, toolchains, python, vcpkg]", sdk)
        for job in ("toolchains", "python", "gcc"):
            block = workflow.split("\n  " + job + ":\n", 1)[1].split("\n  verified:", 1)[0]
            self.assertIn("stage: ${{ fromJSON(needs.plan.outputs." + job + "-matrix) }}", block)
        self.assertIn("python3 scripts/ci-component-plan.py check", workflow)


if __name__ == "__main__":
    unittest.main()

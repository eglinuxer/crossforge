"""Main Python preparation cannot be skipped, broadened, or grant consumer writes."""

import copy
import json
from pathlib import Path
import re
import runpy
import sys
import unittest

from test_ci_component_routing import job

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import ci_execution, ci_python
    from crossforge_internal.identity import IdentityError
    STAGES = runpy.run_path(str(ROOT / "scripts/ci-build.py"))["STAGES"]
finally:
    sys.path.pop(0)


def selection(targets):
    return {"schema_version": 1, "kind": "crossforge-ci-source-plan", "mode": "incremental", "targets": targets}


def outputs(targets, parts):
    return ci_python.execution(json.dumps(selection(targets)), "full", STAGES, json.dumps(parts))


class PythonExecutionTests(unittest.TestCase):
    def test_no_work_single_row_and_sdk_selected_row_have_exact_producer_matrix(self):
        for targets, parts in (({}, {}), ({"toolchain-x86_64": STAGES["toolchain-x86_64"]}, {}),
                ({"python-cp39": STAGES["python-cp39"]}, {"cp39": ["build", "x86_64-install"]}),
                ({"sdk": ["python-cp39-dev"]}, {"cp39": ["build", "x86_64-install"]}),
                ({"sdk": ["sdk-complete-dev"]}, {stage[7:]: ["build"] for stage in ci_execution.GROUPS["python"]})):
            with self.subTest(targets=targets):
                actual = outputs(targets, parts)
                self.assertEqual(actual["python-components"], "true" if parts else "false")
                self.assertEqual(json.loads(actual["python-parts"]), parts)
                matrix = json.loads(actual["python-components-matrix"])["include"]
                self.assertEqual(matrix, [{"row": row, "parts": parts[row]} for row in sorted(parts)] if parts else
                                 [{"row": "cp39", "parts": ["build"]}])
                qualified = sorted(set(targets) & set(ci_execution.GROUPS["python"]))
                self.assertEqual(json.loads(actual["python-rows-matrix"]),
                    {"include": [{"row": stage[7:]} for stage in qualified] or [{"row": "cp39"}]})

    def test_missing_selected_row_unknown_parts_and_unrelated_producers_rejected(self):
        for parts in ({}, {"cp310": ["build"]}, {"cp39": ["build"], "cp310": ["build"]},
                      {"cp39": []}, {"cp39": ["x86_64-install"]}, {"cp39": ["build", "qualification"]},
                      {"cp39": ["build", "build"]}, {"cp39": ["x86_64-install", "build"]},
                      {"cp39": False}, [], None):
            with self.subTest(parts=parts), self.assertRaises(IdentityError):
                outputs({"python-cp39": STAGES["python-cp39"]}, parts)
        with self.assertRaises(IdentityError):
            outputs({}, {"cp39": ["build"]})
        with self.assertRaises(IdentityError):
            outputs({"sdk": ["sdk-complete-dev"]}, {"cp315": ["build"]})

    def test_every_job_result_and_matrix_output_is_required_and_exact(self):
        for targets, parts in (({}, {}), ({"python-cp39": STAGES["python-cp39"]}, {"cp39": ["build"]})):
            output = outputs(targets, parts)
            results = {name: {"result": "success" if output[name] == "true" else "skipped"}
                       for name in list(ci_execution.GROUPS) + ["python-components"]}
            results["plan"] = {"result": "success", "outputs": output}
            self.assertTrue(ci_python.check_results(results, STAGES))
            for name in results:
                for status in ("success", "failure", "skipped", "cancelled", None):
                    if status == results[name]["result"]:
                        continue
                    changed = copy.deepcopy(results)
                    changed[name]["result"] = status
                    self.assertFalse(ci_python.check_results(changed, STAGES), (name, status))
                changed = copy.deepcopy(results)
                del changed[name]
                self.assertFalse(ci_python.check_results(changed, STAGES))
            for key in output:
                changed = copy.deepcopy(results)
                del changed["plan"]["outputs"][key]
                self.assertFalse(ci_python.check_results(changed, STAGES), key)
                changed["plan"]["outputs"][key] = "{}"
                if output[key] != "{}":
                    self.assertFalse(ci_python.check_results(changed, STAGES), key)
            self.assertFalse(ci_python.check_results(dict(results, surprise={"result": "success"}), STAGES))
            changed = copy.deepcopy(results)
            changed["plan"]["outputs"]["extra"] = "true"
            self.assertFalse(ci_python.check_results(changed, STAGES))

    def test_main_consumer_permissions_and_dependencies_keep_gcc_and_vcpkg_independent(self):
        workflow = (ROOT / ".github/workflows/verify-main-builds.yml").read_text()
        for name in ("plan", "inputs", "verified"):
            block = job(workflow, name)
            self.assertIn("permissions:\n      contents: read", block)
            self.assertNotIn("packages:", block)
            self.assertNotIn("id-token:", block)
        for name in ("toolchains", "vcpkg", "gcc", "sdk"):
            block = job(workflow, name)
            self.assertIn("permissions:\n      contents: read\n      packages: read", block)
            self.assertNotIn(": write", block)
            self.assertNotIn("id-token:", block)
            if name == "sdk":
                self.assertIn("uses: ./.github/actions/run-component-sdk", block)
            else:
                self.assertIn("component-reader: true", block)
            self.assertIn("component-builder: ${{ steps.buildx.outputs.builder }}", block)
            self.assertNotIn("python-components: true", block)
            needs = re.search(r"needs: \[(.+)\]", block).group(1).split(", ")
            self.assertEqual("python-components" in needs, name == "sdk")
        qualified = job(workflow, "python")
        self.assertIn("uses: ./.github/workflows/produce-python-row.yml", qualified)
        self.assertIn("matrix: ${{ fromJSON(needs.plan.outputs.python-rows-matrix) }}", qualified)
        self.assertIn("row: ${{ matrix.row }}", qualified)
        self.assertIn("max-parallel: 2", qualified)
        self.assertIn("needs: [plan, inputs, toolchains, python-components]", qualified)
        self.assertIn("packages: write", qualified)
        self.assertIn("id-token: write", qualified)
        self.assertNotIn("run-build-stage", qualified)
        self.assertIn("python, vcpkg", job(workflow, "sdk"))
        producer = job(workflow, "python-components")
        self.assertIn("uses: ./.github/workflows/produce-python.yml", producer)
        self.assertIn("max-parallel: 2", producer)
        self.assertNotIn("vcpkg", producer)
        self.assertNotIn("gcc", producer)
        self.assertIn("packages: write", producer)
        self.assertIn("id-token: write", producer)
        self.assertIn("ci-python.py check", job(workflow, "verified"))
        self.assertIn("checked_source(ROOT, \"main\")", (ROOT / "scripts/ci-python.py").read_text())

    def test_existing_gate_commands_match_readonly_except_explicit_binding_and_freshness(self):
        ordinary = (ROOT / ".github/workflows/verify-incremental.yml").read_text()
        main = (ROOT / ".github/workflows/verify-main-builds.yml").read_text()
        for name in ("inputs", "toolchains", "vcpkg", "gcc", "sdk"):
            before = job(ordinary, name)
            after = job(main, name)
            if name != "inputs":
                after = after.replace("    permissions:\n      contents: read\n      packages: read\n", "", 1)
                after = after.replace("component-reader: true", "component-reader: ${{ inputs.component-reader }}")
            if name in ("toolchains", "vcpkg", "gcc"):
                self.assertEqual(after.count("          replay-qualification: true\n"), 1)
                after = after.replace("          replay-qualification: true\n", "", 1)
            after = after.replace(", python-components", "").replace("          python-components: true\n", "")
            if name == "sdk":
                after = after.replace("uses: ./.github/actions/run-component-sdk", "uses: ./.github/actions/run-build-stage")
                after = after.replace("        with:\n          component-builder:",
                    "        with:\n          component-reader: ${{ inputs.component-reader }}\n          component-builder:")
                after = after.replace("          targets-json:", "          stage: sdk\n          targets-json:")
            self.assertEqual(before, after, name)
        action = (ROOT / ".github/actions/run-build-stage/action.yml").read_text()
        self.assertIn('test "$COMPONENT_READER" = true\n          components+=(--python-components)', action)
        self.assertNotIn("python-components", ordinary)


if __name__ == "__main__":
    unittest.main()

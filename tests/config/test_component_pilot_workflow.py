import json
import os
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/component-pilot.yml").read_text()


class ComponentPilotWorkflowTests(unittest.TestCase):
    def test_manual_main_writer_is_separate_from_readonly_consumer(self):
        self.assertIn("  workflow_dispatch:\n", WORKFLOW)
        self.assertNotIn("  push:", WORKFLOW)
        self.assertNotIn("  pull_request:", WORKFLOW)
        producer = WORKFLOW.split("  produce:\n", 1)[1].split("  consume:\n", 1)[0]
        consumer = WORKFLOW.split("  consume:\n", 1)[1].split("  verified:\n", 1)[0]
        self.assertIn("github.repository == 'eglinuxer/crossforge' && github.ref == 'refs/heads/main'", producer)
        self.assertIn("      packages: write", producer)
        self.assertIn("      packages: read", consumer)
        self.assertNotIn("packages: write", consumer)
        self.assertIn("artifact-ids: ${{ needs.produce.outputs.handoff-artifact-id }}", consumer)
        self.assertIn("HANDOFF_SHA256: ${{ needs.produce.outputs.handoff-sha256 }}", consumer)

    def test_gate_rejects_skipped_cancelled_failed_or_missing_jobs_even_with_python_optimization(self):
        gate = WORKFLOW.split("  verified:\n", 1)[1]
        command = gate.split("        run: |\n", 1)[1].strip()
        expected = {name: {"result": "success"} for name in ("preflight", "produce", "consume")}
        def run(results):
            environment = dict(os.environ, RESULTS=json.dumps(results), PYTHONOPTIMIZE="2")
            return subprocess.run(["bash", "-c", command], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode
        self.assertEqual(run(expected), 0)
        for status in ("skipped", "cancelled", "failure"):
            for name in expected:
                changed = dict(expected, **{name: {"result": status}})
                self.assertNotEqual(run(changed), 0)
        self.assertNotEqual(run({"preflight": expected["preflight"], "consume": expected["consume"]}), 0)
        self.assertNotEqual(run(dict(expected, extra={"result": "success"})), 0)

    def test_actions_are_pinned_and_diagnostics_do_not_upload_large_oci_layouts(self):
        for action in re.findall(r"uses: (\S+)", WORKFLOW):
            self.assertTrue(action.startswith("./") or re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action)
        paths = re.findall(r"^\s+path: (.+)$", WORKFLOW, re.M)
        self.assertIn("${{ runner.temp }}/component-producer/handoff.json", paths)
        self.assertIn("${{ runner.temp }}/component-consumer/report/", paths)
        self.assertNotIn("${{ runner.temp }}/component-consumer/", paths)
        self.assertNotIn("${{ runner.temp }}/component-producer/", paths)


if __name__ == "__main__":
    unittest.main()

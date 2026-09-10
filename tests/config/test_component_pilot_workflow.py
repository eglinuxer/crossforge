import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/component-pilot.yml").read_text()


class ComponentPilotWorkflowTests(unittest.TestCase):
    def test_manual_main_writer_is_separate_from_readonly_consumer(self):
        self.assertIn("  workflow_dispatch:\n", WORKFLOW)
        self.assertNotIn("  push:", WORKFLOW)
        self.assertNotIn("  pull_request:", WORKFLOW)
        producer = WORKFLOW.split("  produce:\n", 1)[1].split("  consume:\n", 1)[0]
        consumer = WORKFLOW.split("  consume:\n", 1)[1].split("  catalog:\n", 1)[0]
        self.assertIn("github.repository == 'eglinuxer/crossforge' && github.ref == 'refs/heads/main'", producer)
        self.assertIn("      packages: write", producer)
        self.assertIn("      packages: read", consumer)
        self.assertNotIn("packages: write", consumer)
        self.assertIn("artifact-ids: ${{ needs.produce.outputs.handoff-artifact-id }}", consumer)
        self.assertIn("HANDOFF_SHA256: ${{ needs.produce.outputs.handoff-sha256 }}", consumer)

    def test_gate_rejects_skipped_cancelled_failed_or_missing_jobs_even_with_python_optimization(self):
        gate = WORKFLOW.split("  verified:\n", 1)[1]
        command = textwrap.dedent(gate.split("        run: |\n", 1)[1])
        jobs = ("preflight", "produce", "consume", "catalog", "catalog-store", "reuse")
        def run(results, mode):
            environment = dict(os.environ, RESULTS=json.dumps(results), MODE=mode, PYTHONOPTIMIZE="2")
            return subprocess.run(["bash", "-c", command], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode
        for mode in ("build", "reuse"):
            selected = {"preflight", "reuse"} if mode == "reuse" else set(jobs) - {"reuse"}
            expected = {name: {"result": "success" if name in selected else "skipped"} for name in jobs}
            self.assertEqual(run(expected, mode), 0)
            for status in ("success", "skipped", "cancelled", "failure"):
                for name in expected:
                    if status != expected[name]["result"]:
                        changed = dict(expected, **{name: {"result": status}})
                        self.assertNotEqual(run(changed, mode), 0)
            self.assertNotEqual(run({"preflight": expected["preflight"], "consume": expected["consume"]}, mode), 0)
            self.assertNotEqual(run(dict(expected, extra={"result": "success"}), mode), 0)
            self.assertNotEqual(run(expected, "unknown"), 0)

    def test_actions_are_pinned_and_diagnostics_do_not_upload_large_oci_layouts(self):
        for action in re.findall(r"uses: (\S+)", WORKFLOW):
            self.assertTrue(action.startswith("./") or re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), action)
        paths = re.findall(r"^\s+path: (.+)$", WORKFLOW, re.M)
        self.assertIn("${{ runner.temp }}/component-producer/handoff.json", paths)
        self.assertIn("${{ runner.temp }}/component-consumer/report/", paths)
        self.assertNotIn("${{ runner.temp }}/component-consumer/", paths)
        self.assertNotIn("${{ runner.temp }}/component-producer/", paths)

    def test_catalog_signer_runs_after_gates_and_has_no_registry_write_permission(self):
        signer = WORKFLOW.split("  catalog:\n", 1)[1].split("  catalog-store:\n", 1)[0]
        self.assertIn("needs: [produce, consume]", signer)
        self.assertNotIn("if: always()", signer)
        self.assertIn("id-token: write", signer)
        self.assertNotIn("packages: write", signer)
        self.assertNotIn("id-token: write", WORKFLOW.split("  catalog:\n", 1)[0])
        self.assertIn("artifact-ids: ${{ needs.produce.outputs.handoff-artifact-id }}", signer)
        self.assertIn("HANDOFF_SHA256: ${{ needs.produce.outputs.handoff-sha256 }}", signer)
        self.assertIn("component-catalog.py from-handoff", signer)
        self.assertIn('--bundle "$output/catalog.sigstore.json" "$output/catalog.json"', signer)
        self.assertIn("component-catalog.py verify", signer)
        self.assertIn('validate-sigstore-report.py "$output/sigstore-verification.json"', signer)

    def test_catalog_storage_reverifies_signature_without_oidc_permission(self):
        storage = WORKFLOW.split("  catalog-store:\n", 1)[1].split("  reuse:\n", 1)[0]
        self.assertIn("needs: catalog", storage)
        self.assertIn("packages: write", storage)
        self.assertNotIn("id-token: write", storage)
        self.assertIn("artifact-ids: ${{ needs.catalog.outputs.artifact-id }}", storage)
        self.assertIn("component-catalog.py publish", storage)
        self.assertIn('validate-sigstore-report.py "$output/sigstore-verification.json"', storage)
        self.assertIn('reference: ${{ steps.store.outputs.reference }}', storage)
        self.assertIn("docker logout ghcr.io", storage)

    def test_reuse_mode_only_reads_prior_catalogs_and_runs_the_same_consumer_gates(self):
        reuse = WORKFLOW.split("  reuse:\n", 1)[1].split("  verified:\n", 1)[0]
        producer = WORKFLOW.split("  produce:\n", 1)[1].split("  consume:\n", 1)[0]
        self.assertIn("inputs.mode == 'reuse'", reuse)
        self.assertIn("inputs.mode == 'build'", producer)
        self.assertIn('run: test -z "$CATALOG_REFERENCE"', producer)
        self.assertIn("packages: read", reuse)
        self.assertNotIn("packages: write", reuse)
        self.assertNotIn("id-token: write", reuse)
        self.assertNotIn("sign-blob", reuse)
        self.assertIn('component-pilot.py consume-catalog', reuse)
        self.assertIn('--catalog-reference "$CATALOG_REFERENCE"', reuse)
        self.assertIn("${{ runner.temp }}/component-reuse/report/", reuse)
        self.assertNotIn("${{ runner.temp }}/component-reuse/\n", reuse)


if __name__ == "__main__":
    unittest.main()

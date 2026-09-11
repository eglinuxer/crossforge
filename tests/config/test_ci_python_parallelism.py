"""Run the real scheduling guards while preserving job and receipt boundaries."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_ci_component_routing import job

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return (ROOT / ".github/workflows" / (name + ".yml")).read_text()


def body(text, name):
    step = text.split("      - name: " + name + "\n", 1)[1].split("\n      - ", 1)[0]
    return "\n".join(line[10:] for line in step.split("        run: |\n", 1)[1].splitlines())


class PythonParallelismTests(unittest.TestCase):
    def test_both_reusable_boundaries_reject_other_values_before_selecting_work(self):
        early = workflow("verify-main-incremental")
        main = workflow("verify-main-builds")
        guards = [body(early, "Validate Python parallelism before component planning"),
                  body(main, "Record controlled Python parallelism")]
        self.assertLess(early.index("Validate Python parallelism"), early.index("uses: ./.github/actions/setup-locked-buildx"))
        self.assertLess(main.index("Record controlled Python parallelism"), main.index("- name: Validate selected work"))
        for script in guards:
            for value in ("2", "3", "", "0", "1", "4", "2.5", "02", "-1", "true", "2\n3", "2; exit 0"):
                with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                    environment = dict(os.environ, PYTHON_PARALLELISM=value, RUNNER_TEMP=temporary,
                        GITHUB_SHA="a" * 40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", GITHUB_EVENT_NAME="workflow_dispatch")
                    result = subprocess.run(["bash", "-Eeuo", "pipefail", "-c", script],
                        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.assertEqual(result.returncode == 0, value in ("2", "3"), result.stderr)
                    path = Path(temporary) / "python-parallelism.json"
                    if value in ("2", "3") and script == guards[1]:
                        record = json.loads(path.read_text())
                        self.assertEqual(record, {"schema_version": 1, "kind": "crossforge-python-scheduling-observation",
                            "python_parallelism": int(value), "source_commit": "a" * 40, "run_id": "123", "attempt": "2",
                            "event": "workflow_dispatch", "performance_result": "not asserted"})
                        duplicate = subprocess.run(["bash", "-Eeuo", "pipefail", "-c", script],
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        self.assertNotEqual(duplicate.returncode, 0)
                        self.assertEqual(json.loads(path.read_text()), record)
                    else:
                        self.assertFalse(path.exists())

    def test_default_two_flows_only_to_main_python_matrices_and_has_an_observation(self):
        ci, wrapper, main = [workflow(name) for name in ("ci", "verify-main-incremental", "verify-main-builds")]
        dispatch = ci.split("  workflow_dispatch:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("default: '2'", dispatch)
        self.assertIn("options: ['2', '3']", dispatch)
        self.assertIn("python-parallelism: ${{ fromJSON(inputs.python-parallelism || '2') }}", job(ci, "builds-components"))
        self.assertNotIn("python-parallelism", job(ci, "builds-readonly"))
        self.assertIn("python-parallelism: ${{ inputs.python-parallelism }}", job(wrapper, "builds"))
        for text in (wrapper, main):
            definition = text.split("      python-parallelism:\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("type: number", definition)
            self.assertIn("default: 2", definition)
        for name in ("python-components", "python"):
            self.assertIn("max-parallel: ${{ inputs.python-parallelism }}", job(main, name))
            self.assertIn("fail-fast: false", job(main, name))
        for name in ("toolchains", "gcc"):
            self.assertIn("max-parallel: 2", job(main, name))
        self.assertNotIn("python-parallelism", workflow("candidate"))
        self.assertIn("name: python-scheduling-${{ github.run_id }}-${{ github.run_attempt }}", job(main, "plan"))
        self.assertIn("if-no-files-found: error", job(main, "plan"))

"""Main SDK orchestration uses real acquisition control flow with explicit domain mocks."""

import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import unittest
from unittest import mock

import test_python_sdk_catalog as fixtures
import test_component_recovery as producer_fixtures

from crossforge_internal import ci_sdk, component_ci, component_recovery, python_qualification
from crossforge_internal.identity import IdentityError, content_sha256, load_json

ROOT = fixtures.ROOT
CLI = runpy.run_path(str(ROOT / "scripts/ci-sdk.py"))


class MainSdkTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.PythonSdkCatalogTests.setUpClass.__func__)
    patch = fixtures.PythonSdkCatalogTests.patch

    def setUp(self):
        fixtures.PythonSdkCatalogTests.setUp(self)
        self.producer = producer_fixtures.selection()["producer"]
        expected = component_recovery.requirements(ROOT, self.graph, True)
        for group in (self.toolchains, self.python):
            for name, result in group["components"].items():
                subject = result["subject"]
                subject.setdefault("receipt", "/fixture/" + name + "/receipt.json")
                subject.setdefault("receipt_sha256", "d" * 64)
                subject.setdefault("layout", "/fixture/" + name + "/oci")
                result.update(producer_fixtures.selection(**expected[name]), subject=subject)
        self.source = self.patch(component_ci, "checked_source", return_value=self.producer)
        self.graph_reader = self.patch(component_ci, "source_graph", return_value=self.graph)
        self.qualified = {row: {"status": "verified-qualified-row", "subject": value["qualification"],
            "producer": {"fixture": "original row producer"}} for row, value in self.components["rows"].items()}
        self.row_resolver.side_effect = lambda *args, **kwargs: self.qualified[args[2]]
        self.produce = self.patch(python_qualification, "produce", side_effect=self.produced)
        self.integrate = self.patch(fixtures.python_sdk, "execute", return_value={"fixture": "fresh SDK integration"})
        def worker(command, **kwargs):
            self.assertEqual(command[1:3], [str(ROOT / "scripts/ci-sdk.py"), "qualify-row"])
            self.assertTrue(kwargs["check"])
            return ci_sdk.qualify_request(Path(command[4]), command[6])
        self.worker = self.patch(ci_sdk.subprocess, "run", side_effect=worker)

    def produced(self, source, graph, row, execution, producer, subjects, data, *args):
        self.assertEqual(execution, self.execution)
        self.assertEqual(producer, self.producer)
        self.assertEqual(subjects, self.components["rows"][row]["subjects"])
        receipt = {"artifact": {"root_digest": "sha256:" + "a" * 64}, "contract": {"producer": producer}}
        fixtures.component_build.write_json(data / "receipt.json", receipt)
        return {"inputs_sha256": self.qualified[row]["inputs_sha256"], "receipt_sha256": content_sha256(receipt),
                "artifact": receipt["artifact"]}

    def execute(self, targets=None):
        return ci_sdk.execute(ROOT, targets or ["python-matrix", "sdk-complete-dev"], self.data, self.evidence,
            "builder", self.root / "oras", self.root / "cosign")

    def miss(self, row):
        self.qualified[row] = {"status": "qualification-required", "inputs_sha256": "b" * 64}

    def test_matching_catalog_rows_preserve_producers_and_always_run_final_integration(self):
        value = self.execute()
        self.assertEqual(value["root"], "sdk-complete-dev")
        self.assertEqual(self.graph_reader.call_args[0][1], ["sdk-complete-dev"])
        self.assertEqual(self.integrate.call_args[0][4], self.components)
        self.assertEqual(value["integration"], {"fixture": "fresh SDK integration"})
        self.assertTrue(all(row["origin"] == "authenticated-catalog" for row in value["rows"].values()))
        self.assertTrue(all(row["producer"] == {"fixture": "original row producer"} for row in value["rows"].values()))
        self.produce.assert_not_called()
        self.assertEqual(load_json(self.evidence / "result.json"), value)

    def test_only_missing_rows_are_freshly_qualified_on_the_integration_builder(self):
        self.miss("cp39")
        result = self.execute(["python-dev"])
        self.produce.assert_called_once()
        self.assertEqual(self.produce.call_args[0][2], "cp39")
        self.assertEqual(self.produce.call_args[0][7], self.integrate.call_args[0][6])
        components = self.integrate.call_args[0][4]
        self.assertEqual(components["rows"]["cp39"]["qualification"]["receipt"], str(self.data / "fresh/cp39/receipt.json"))
        self.assertEqual(result["rows"]["cp39"]["origin"], "fresh-execution")
        self.assertEqual(result["rows"]["cp39"]["producer"], self.producer)
        self.assertEqual(sum(row["origin"] == "authenticated-catalog" for row in result["rows"].values()), 5)
        self.worker.assert_called_once()
        request = load_json(self.data / "requests/cp39.json")
        self.assertEqual(request["producer"], self.producer)

    def test_all_missing_rows_keep_two_workers_and_complete_before_integration(self):
        for row in self.qualified:
            self.miss(row)
        barrier, lock = threading.Barrier(2), threading.Lock()
        counts = {"active": 0, "maximum": 0, "completed": 0}
        def produce(*args):
            with lock:
                counts["active"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
            barrier.wait(timeout=5)
            result = self.produced(*args)
            with lock:
                counts["active"] -= 1
                counts["completed"] += 1
            return result
        self.produce.side_effect = produce
        def integrate(*args):
            self.assertEqual(counts["completed"], 6)
            return {"fixture": "integration after six rows"}
        self.integrate.side_effect = integrate
        self.execute()
        self.assertEqual(counts["maximum"], 2)

    def test_missing_raw_component_cannot_trigger_implicit_source_builds_or_row_qualification(self):
        self.python["components"]["cp39-build"] = {"status": "build-required"}
        self.python["required_producers"] = [fixtures.python_components.spec(ROOT, "cp39", "build", "install")["target"]]
        with self.assertRaisesRegex(IdentityError, "raw component producers"):
            self.execute()
        self.produce.assert_not_called()
        self.integrate.assert_not_called()

    def test_authentication_transport_or_prior_row_validation_errors_do_not_fall_back(self):
        self.row_resolver.side_effect = IdentityError("fixture rejected catalog or execution evidence")
        with self.assertRaises(IdentityError):
            self.execute()
        self.produce.assert_not_called()
        self.integrate.assert_not_called()
        self.assertFalse((self.evidence / "result.json").exists())

    def test_untrusted_source_fails_before_any_catalog_or_build_access(self):
        self.source.side_effect = IdentityError("fixture untrusted caller")
        with self.assertRaises(IdentityError):
            self.execute()
        self.graph_reader.assert_not_called()
        self.toolchain_resolver.assert_not_called()
        self.produce.assert_not_called()

    def test_changed_fresh_inputs_preserve_diagnostics_but_prevent_integration(self):
        self.miss("cp39")
        def mismatched(*args):
            return dict(self.produced(*args), inputs_sha256="c" * 64)
        self.produce.side_effect = mismatched
        with self.assertRaisesRegex(IdentityError, "planned inputs"):
            self.execute()
        self.assertTrue((self.evidence / "rows/cp39/receipt.json").is_file())
        self.integrate.assert_not_called()
        self.assertFalse((self.evidence / "result.json").exists())

    def test_failed_local_qualification_does_not_integrate_or_publish_success(self):
        self.miss("cp39")
        self.produce.side_effect = IdentityError("fixture failed runtime gate")
        with self.assertRaises(IdentityError):
            self.execute()
        self.integrate.assert_not_called()
        self.assertFalse((self.evidence / "result.json").exists())

    def test_final_integration_failure_does_not_write_a_success_result(self):
        self.integrate.side_effect = IdentityError("fixture failed final SDK")
        with self.assertRaises(IdentityError):
            self.execute()
        self.assertTrue((self.evidence / "rows.json").is_file())
        self.assertFalse((self.evidence / "result.json").exists())
        saved = load_json(self.evidence / "component-recovery.json")
        selected = load_json(self.evidence / "recovery-reference.json")
        expected = component_recovery.requirements(ROOT, self.graph, True)
        context = component_recovery.context(ROOT, self.graph, "sdk", ["python-dev", "sdk-complete-dev"],
            self.execution["build"], self.producer["source_commit"])
        self.assertEqual(len(component_recovery.verify(saved, selected["sha256"], context, expected)), 32)

    def test_worker_request_tampering_fails_before_qualification(self):
        path = self.root / "request.json"
        request = dict.fromkeys(ci_sdk.ROW_JOB_FIELDS)
        fixtures.component_build.write_json(path, request)
        with self.assertRaisesRegex(IdentityError, "selected SHA256"):
            ci_sdk.qualify_request(path, "0" * 64)
        self.produce.assert_not_called()

    def test_post_integration_source_drift_prevents_success(self):
        def drift(*args):
            self.source.return_value = dict(self.producer, source_commit="0" * 40)
            return {"fixture": "integration"}
        self.integrate.side_effect = drift
        with self.assertRaisesRegex(IdentityError, "source or invocation changed"):
            self.execute()
        self.assertFalse((self.evidence / "result.json").exists())

    def test_post_integration_host_drift_prevents_success(self):
        def drift(*args):
            self.environment.return_value = dict(self.execution, host={"fixture": "changed CPU"})
            return {"fixture": "integration"}
        self.integrate.side_effect = drift
        with self.assertRaisesRegex(IdentityError, "execution environment changed"):
            self.execute()
        self.assertFalse((self.evidence / "result.json").exists())

    def test_invalid_or_duplicate_selected_roots_are_rejected_before_source_access(self):
        for targets in ([], ["python-cp39-dev"], ["python-dev", "python-dev"], ["sdk-complete-dev", "unknown"], "sdk-complete-dev"):
            with self.subTest(targets=targets), self.assertRaises(IdentityError):
                ci_sdk.selected_root(targets)
        self.assertEqual(ci_sdk.selected_root(["python-matrix"]), "python-dev")
        self.source.assert_not_called()


class MainSdkCliTests(unittest.TestCase):
    setUp = fixtures.PythonSdkCatalogCliTests.setUp

    def args(self):
        return ["--targets-json", '["sdk-complete-dev"]', "--builder", "fixture", "--oras", str(self.root / "oras"),
                "--cosign", str(self.root / "cosign"), "--component-directory", str(self.root / "data"),
                "--output", str(self.root / "diagnostics/execution")]

    def test_cli_records_success_or_failure_and_stops_resource_monitoring(self):
        for failure in (False, True):
            args = self.args()
            output = self.root / ("failed" if failure else "successful") / "execution"
            args[-1] = str(output)
            with mock.patch.object(ci_sdk, "execute", side_effect=IdentityError("fixture error") if failure else None,
                    return_value={"fixture": "integrated"}) as execute, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(CLI["main"](args), 1 if failure else 0)
            self.assertEqual(load_json(output.parent / "run.json")["exit_code"], int(failure))
            self.assertEqual(execute.call_args[0][1], ["sdk-complete-dev"])
            self.assertTrue((output.parent / "resources.jsonl").is_file())

    def test_reusing_diagnostics_fails_before_appending_or_executing(self):
        with mock.patch.object(ci_sdk, "execute", return_value={"fixture": "integrated"}) as execute, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI["main"](self.args()), 0)
            saved = {name: (self.root / "diagnostics" / name).read_bytes() for name in ("run.json", "resources.jsonl")}
            execute.reset_mock()
            self.assertEqual(CLI["main"](self.args()), 1)
            execute.assert_not_called()
        self.assertEqual(saved, {name: (self.root / "diagnostics" / name).read_bytes() for name in saved})

    def test_actual_worker_entry_rejects_changed_request_before_source_or_docker_access(self):
        request = dict.fromkeys(ci_sdk.ROW_JOB_FIELDS)
        selected = content_sha256(request)
        request["row"] = "cp39"
        path = self.root / "worker.json"
        path.write_text(json.dumps(request))
        result = subprocess.run([sys.executable, str(ROOT / "scripts/ci-sdk.py"), "qualify-row",
            "--request", str(path), "--request-sha256", selected], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"selected SHA256", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_main_action_keeps_read_only_packages_heartbeat_and_separate_diagnostics(self):
        action = (ROOT / ".github/actions/run-component-sdk/action.yml").read_text()
        self.assertIn("setup-component-reader", action)
        self.assertIn("scripts/run-with-heartbeat.py", action)
        self.assertIn('"$RUNNER_TEMP/component-data/sdk"', action)
        self.assertIn("path: ${{ runner.temp }}/build-diagnostics/sdk/", action)
        self.assertNotIn("sign-blob", action)
        self.assertNotIn("registry_transfer.publish", (ROOT / "scripts/crossforge_internal/ci_sdk.py").read_text())
        self.assertIn("if: always()", action)
        self.assertIn("docker logout ghcr.io", action)


if __name__ == "__main__":
    unittest.main()

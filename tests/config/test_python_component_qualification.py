"""Row receipt acceptance, material boundaries and fresh execution coverage."""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import python_qualification as qualification, python_components
    from crossforge_internal import component_artifacts, component_build, component_inputs
    from crossforge_internal.identity import IdentityError, content_sha256
finally:
    sys.path.pop(0)


class PythonQualificationInputsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "--print",
            "python-row-cp39", "toolchain-x86_64-build-export", "toolchain-aarch64-build-export"], cwd=ROOT))

    def setUp(self):
        self.execution = {"build": {"buildkit_image": "moby/buildkit:test@sha256:" + "a" * 64},
                          "host": {"kernel": "fixture"}}
        subjects = {"build": {}}
        subjects.update({arch + "-" + kind: {} for arch in python_components.ARCHES
                         for kind in ("install", "test-context", "toolchain")})
        def verified(subject, expected, role, target, *args):
            digest = "sha256:" + hashlib.sha256(expected["component"].encode()).hexdigest()
            return "oci-layout:///fixture@" + digest, {"component": expected["component"],
                "inputs_sha256": component_inputs.identity(expected), "artifact_digest": digest}
        with mock.patch.object(python_components, "verify", side_effect=verified):
            self.resolved, self.bindings = python_components.bind_row(ROOT, self.graph, "cp39", self.execution["build"], subjects, "fixture")
        self.settings = qualification.spec(ROOT, "cp39")

    def capture(self, root=ROOT):
        return qualification.inputs(root, self.resolved, self.settings, self.execution, self.bindings)

    def test_seven_subjects_and_every_required_run_are_bound_without_compilers(self):
        value = self.capture()
        self.assertEqual(len(value["dependencies"]), 7)
        self.assertEqual(value["parameters"]["required_runs"], {
            "cpython-row-assemble": 1, "cpython-qualify-build": 4,
            "cpython-qualify-x86_64": 1, "cpython-qualify-aarch64": 1})
        self.assertEqual(len(value["parameters"]["bake_targets"]), 9)
        paths = {record["path"] for record in value["files"]}
        self.assertFalse(paths & {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"})
        self.assertIn("scripts/crossforge_internal/python_qualification.py", paths)
        self.assertIn("scripts/python_sdk_identity.py", paths)

    def test_host_inspector_and_each_actual_subject_change_identity(self):
        original = self.capture()
        for key in self.bindings:
            previous = self.bindings[key]["inputs_sha256"]
            self.bindings[key]["inputs_sha256"] = "0" * 64
            # Repeated contexts for one component must agree or fail closed.
            try:
                self.assertNotEqual(original, self.capture())
            except IdentityError:
                pass
            self.bindings[key]["inputs_sha256"] = previous
        self.execution["host"]["kernel"] = "changed"
        self.assertNotEqual(original, self.capture())
        self.execution["host"]["kernel"] = "fixture"
        with mock.patch.object(qualification, "inspection_identity", return_value={"python": "changed"}):
            self.assertNotEqual(original, self.capture())

    def test_wrong_row_stage_missing_host_and_unverified_source_are_rejected(self):
        original = copy.deepcopy(self.resolved)
        self.resolved["target"][self.settings["target"]]["target"] = "cpython-row-assemble"
        with self.assertRaises(IdentityError):
            self.capture()
        self.resolved = original
        self.execution["host"] = {}
        with self.assertRaises(IdentityError):
            self.capture()
        self.execution["host"] = {"kernel": "fixture"}
        self.resolved = self.graph
        self.bindings = {}
        with self.assertRaisesRegex(IdentityError, "seven verified subjects"):
            self.capture()


class PythonQualificationReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = qualification.spec(ROOT, "cp39")
        self.coverage = {"row": "cp39", "status": "passed", "fixture": "not real qualification"}
        self.inspection = qualification.inspection_identity()
        self.inputs = component_inputs.capture(ROOT, self.settings["component"], "qualification", [".dockerignore"],
            self.settings["targets"], {"qualification": self.settings, "inspection": self.inspection,
                "required_runs": {"cpython-row-assemble": 1},
                "recipes": {self.settings["target"]: {"frontend": "docker/dockerfile:1@sha256:" + "a" * 64}}})
        self.producer = {"kind": "local", "source_commit": "b" * 40, "source_dirty": True,
                         "invocation": "urn:crossforge:local:fixture", "started_at": "2026-09-10T00:00:00Z"}
        self.contract = component_artifacts.contract("qualification", self.inputs, self.producer)
        component_build.write_json(self.root / component_artifacts.CONTRACT_PATH, self.contract)
        for name in qualification.REPORTS:
            component_build.write_json(self.root / "component/reports" / name, {})
        self.progress = {"vertexes": [{"digest": "sha256:" + "c" * 64,
            "name": "[row cpython-row-assemble 9/9] RUN /finalize-python-row.py",
            "started": "2026-09-10T00:00:01Z", "completed": "2026-09-10T00:00:02Z"}]}
        self.observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
                            "root_digest": "sha256:" + "d" * 64, "platform_digest": "sha256:" + "d" * 64,
                            "config_digest": "sha256:" + "e" * 64}

    def envelope(self, changes=None):
        (self.root / qualification.PROGRESS_PATH).write_text(json.dumps(self.progress) + "\n")
        with mock.patch.object(qualification, "validate_row", return_value=self.coverage):
            record = qualification.record(self.contract, ROOT, self.root, "2026-09-10T00:00:00Z", "2026-09-10T00:00:03Z")
        record.update(changes or {})
        (self.root / qualification.RECORD_PATH).write_text(json.dumps(record) + "\n")
        return component_artifacts.receipt(self.contract, self.observation, self.root, qualification.metadata_paths()), record

    def verify(self, receipt, expected=None, trusted=None):
        with mock.patch.object(qualification.oci_layout, "inspect", return_value=self.observation), \
             mock.patch.object(qualification, "extract_row", return_value=self.root), \
             mock.patch.object(qualification, "validate_row", return_value=self.coverage) as validate:
            result = qualification.verify_local(receipt, trusted or content_sha256(receipt), expected or self.inputs,
                ROOT, self.root / "oci", "fixture", temporary_parent=self.root)
            validate.assert_called_once()
            return result

    def test_verified_prior_record_retains_original_producer_interval_and_coverage(self):
        receipt, record = self.envelope()
        result = self.verify(receipt)
        self.assertEqual(result["mode"], "verified-prior-execution")
        self.assertEqual(result["qualification"], record)
        self.assertEqual(result["qualification"]["producer"], self.producer)

    def test_receipt_trust_inputs_role_and_missing_evidence_fail_closed(self):
        receipt, _ = self.envelope()
        with self.assertRaises(IdentityError):
            self.verify(receipt, trusted="0" * 64)
        changed = copy.deepcopy(self.inputs)
        changed["parameters"]["inspection"]["python"] = "different"
        with self.assertRaises(IdentityError):
            self.verify(receipt, expected=changed)
        for path in (qualification.RECORD_PATH, qualification.PROGRESS_PATH, "component/reports/aarch64.json"):
            changed = copy.deepcopy(receipt)
            changed["metadata"] = [item for item in changed["metadata"] if item["path"] != path]
            with self.assertRaisesRegex(IdentityError, "evidence set differs"):
                self.verify(changed)

    def test_resealed_unknown_incomplete_or_relabelled_execution_is_rejected(self):
        for change in ({"coverage": {}}, {"mode": "reused"}, {"schema_version": True},
                       {"producer": dict(self.producer, invocation="urn:crossforge:local:new")}, {"extra": True}):
            receipt, _ = self.envelope(change)
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.verify(receipt)

    def test_cached_progress_cannot_be_resealed_as_fresh_execution(self):
        receipt, _ = self.envelope()
        self.progress["vertexes"][0]["cached"] = True
        (self.root / qualification.PROGRESS_PATH).write_text(json.dumps(self.progress) + "\n")
        receipt = component_artifacts.receipt(self.contract, self.observation, self.root, qualification.metadata_paths())
        with self.assertRaisesRegex(IdentityError, "cached or failed"):
            self.verify(receipt)

    def test_actual_row_validation_rejects_mirror_mismatch_and_invalid_installed_files(self):
        reports = self.root / self.settings["copies"][1]
        reports.mkdir(parents=True)
        for name in qualification.REPORTS:
            shutil.copy2(str(self.root / "component/reports" / name), str(reports / name))
        (reports / "aarch64.json").write_text('{"different":true}')
        with self.assertRaisesRegex(IdentityError, "row report differs"):
            qualification.validate_row(ROOT, self.settings, self.root, self.root)
        shutil.copy2(str(self.root / "component/reports/aarch64.json"), str(reports / "aarch64.json"))
        # Execute the real finalizer; empty claims without SDK files cannot pass.
        with self.assertRaises(subprocess.CalledProcessError):
            qualification.validate_row(ROOT, self.settings, self.root, self.root)


if __name__ == "__main__":
    unittest.main()

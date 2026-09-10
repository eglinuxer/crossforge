import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_qualification as qualification
    from crossforge_internal import component_artifacts, component_inputs
    from crossforge_internal.identity import IdentityError, content_sha256, file_record, load_json
finally:
    sys.path.pop(0)


class QualificationInputsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        (self.root / "docker").mkdir()
        (self.root / ".dockerignore").write_text(".git/\n")
        (self.root / "test.c").write_text("int main(){return 0;}\n")
        (self.root / "compiler.c").write_text("expensive compiler input")
        (self.root / "docker/Dockerfile").write_text("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 + "\n"
            "FROM scratch AS source-compiler\nCOPY compiler.c /compiler.c\nRUN /build-compiler\n"
            "FROM crossforge_toolchain_x86_64_install AS toolchain-x86_64-qualify-build\n"
            "COPY test.c /test.c\nRUN /test\n"
            "FROM toolchain-x86_64-qualify-build AS runtime-smoke-x86_64\nRUN /runtime\n"
            "FROM runtime-smoke-x86_64 AS toolchain-x86_64-dev\n")
        self.settings = qualification.spec("x86_64", "toolchain")
        self.execution = {"build": {"buildkit_image": "image@sha256:" + "b" * 64}, "host": {"kernel": "fixture"}}
        self.bindings = {"crossforge_toolchain_x86_64_install": {"component": "toolchain/x86_64-install",
            "inputs_sha256": "c" * 64, "artifact_digest": "sha256:" + "d" * 64}}
        self.graph = {"target": {"toolchain-x86_64-dev": {"context": ".", "dockerfile": "docker/Dockerfile",
            "target": "toolchain-x86_64-dev", "platforms": ["linux/amd64"], "contexts": {
                "crossforge_toolchain_x86_64_install": "oci-layout:///local@sha256:" + "d" * 64}}}}

    def capture(self):
        return qualification.qualification_inputs(self.root, self.graph, self.settings, self.execution, self.bindings)

    def test_qualification_stops_at_verified_artifact_and_binds_test_and_host(self):
        baseline = self.capture()
        self.assertEqual(baseline["dependencies"], list(self.bindings.values()))
        self.assertNotIn("compiler.c", {record["path"] for record in baseline["files"]})
        (self.root / "compiler.c").write_text("compiler producer changed")
        self.assertEqual(baseline, self.capture())
        (self.root / "test.c").write_text("test changed")
        self.assertNotEqual(baseline, self.capture())
        baseline = self.capture()
        self.execution["host"]["kernel"] = "other"
        self.assertNotEqual(baseline, self.capture())
        self.assertEqual(baseline["parameters"]["required_runs"], {
            "toolchain-x86_64-qualify-build": 1, "runtime-smoke-x86_64": 1})

    def test_report_acceptance_implementation_is_qualification_material(self):
        baseline = self.capture()
        path = self.root / "scripts/toolchain_report.py"
        path.write_text(path.read_text() + "\n# acceptance changed\n")
        self.assertNotEqual(baseline, self.capture())

    def test_missing_host_context_and_wrong_artifact_digest_fail(self):
        self.execution.pop("host")
        with self.assertRaises(IdentityError):
            self.capture()
        self.execution["host"] = {"kernel": "fixture"}
        self.bindings["crossforge_toolchain_x86_64_install"]["artifact_digest"] = "sha256:" + "e" * 64
        with self.assertRaises(IdentityError):
            self.capture()

    def test_wrong_subject_roles_and_environment_rejected_before_build(self):
        with mock.patch.object(qualification.component_build, "verify_local") as verify:
            with self.assertRaises(IdentityError):
                qualification.bind_subjects(self.root, self.graph, self.settings, self.execution,
                    {"gcc-test-context": {}}, "builder")
            verify.assert_not_called()
        with mock.patch.object(qualification.qualification_execution, "execution_identity", return_value={}):
            with self.assertRaises(IdentityError):
                qualification.produce(self.root, self.graph, "x86_64", "toolchain", self.execution,
                    {}, {}, self.root / "out", "builder")
            self.assertFalse((self.root / "out").exists())

    def test_gcc_full_arm_is_not_invented_and_smoke_requires_both_subjects(self):
        with self.assertRaises(IdentityError):
            qualification.spec("aarch64", "gcc-full")
        self.assertEqual(set(qualification.spec("x86_64", "gcc-smoke")["contexts"].values()),
                         {"toolchain-install", "gcc-test-context"})


class GccQualificationReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = qualification.spec("x86_64", "gcc-smoke")
        self.reports = self.root / "component/reports"
        self.directory = self.reports / "x86_64-host-direct"
        self.directory.mkdir(parents=True)
        self.summary = self.directory / "gcc.execute.sum"
        self.summary.write_text("PASS: fixture\n")
        (self.directory / "gcc.execute.make.log").write_text("make log\n")
        (self.directory / "gcc.execute.log").write_text("DejaGNU log\n")
        validator, policy = qualification._gcc_profile(ROOT, "smoke")
        board = policy["plan"]["targets"][0]["runtime_tiers"][0]["board"]
        component = load_json(ROOT / "config/generated/components/toolchain/gcc-testsuite-qualification.json")
        materials = {"qualification_component": {"component": component["component"], "canonical_sha256": content_sha256(component)},
            "gcc": {"version": policy["plan"]["gcc_version"]}, "site_sha256": policy["plan"]["site"]["sha256"],
            "board": {key: board[key] for key in ("name", "sha256")},
            "make": [{"suite": "gcc.execute", "log_sha256": file_record(self.directory, "gcc.execute.make.log")["sha256"]}]}
        self.report = validator["normalize_summaries"](policy["plan"],
            policy["baselines"][("x86_64-unknown-linux-gnu", "host-direct")]["document"],
            {"gcc.execute": self.summary}, materials)
        self.path = self.reports / "x86_64-host-direct.json"

    def check(self):
        self.path.write_text(json.dumps(self.report))
        return qualification.validate_reports(ROOT, self.settings, self.root)

    def test_exact_baseline_normalization_and_execution_materials_pass(self):
        self.assertEqual(self.check()[0]["status_counts"], {"PASS": 1})

    def test_report_tampering_or_missing_execution_materials_fail(self):
        for change in ({"make": []}, {"board": {}}, {"qualification_component": {}}, {"site_sha256": "0" * 64}):
            original = copy.deepcopy(self.report)
            self.report["materials"].update(change)
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.check()
            self.report = original
        self.report["status_counts"] = {"PASS": 999}
        with self.assertRaises(IdentityError):
            self.check()
        self.report["status_counts"] = {"PASS": True}
        with self.assertRaises(IdentityError):
            self.check()

    def test_added_failure_and_modified_make_log_are_rejected(self):
        self.summary.write_text("PASS: fixture\nFAIL: added failure\n")
        with self.assertRaises(IdentityError):
            self.check()

    def envelope(self, change=None):
        self.check()
        frontend = "docker/dockerfile:1@sha256:" + "a" * 64
        parameters = {"qualification": self.settings, "report_copies": qualification.report_copies(ROOT, self.settings),
                      "required_runs": {"gcc-testsuite-x86_64-smoke": 1},
                      "recipes": {self.settings["target"]: {"frontend": frontend}}}
        inputs = component_inputs.capture(ROOT, self.settings["component"], "qualification", [".dockerignore"],
            [self.settings["triple"]], parameters)
        producer = {"kind": "local", "source_commit": "a" * 40, "source_dirty": True,
                    "invocation": "urn:crossforge:local:fixture", "started_at": "2026-09-10T00:00:00Z"}
        contract = component_artifacts.contract("qualification", inputs, producer)
        (self.root / component_artifacts.CONTRACT_PATH).write_text(json.dumps(contract))
        progress = {"vertexes": [{"digest": "sha256:" + "b" * 64,
            "name": "[gate gcc-testsuite-x86_64-smoke 2/2] RUN /run-gcc-testsuite.py",
            "started": "2026-09-10T00:00:01Z", "completed": "2026-09-10T00:00:02Z"}]}
        (self.root / qualification.PROGRESS_PATH).write_text(json.dumps(progress) + "\n")
        record = qualification._record(contract, ROOT, self.root, "2026-09-10T00:00:00Z", "2026-09-10T00:00:03Z")
        if change:
            record.update(change)
        (self.root / qualification.RECORD_PATH).write_text(json.dumps(record))
        observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
                       "root_digest": "sha256:" + "c" * 64, "platform_digest": "sha256:" + "c" * 64,
                       "config_digest": "sha256:" + "d" * 64}
        paths = [component_artifacts.CONTRACT_PATH, qualification.RECORD_PATH, qualification.PROGRESS_PATH] + list(parameters["report_copies"].values())
        receipt = component_artifacts.receipt(contract, observation, self.root, paths)
        return receipt, observation, inputs, record

    def verify_envelope(self, receipt, observation, inputs):
        with mock.patch.object(qualification.oci_layout, "inspect", return_value=observation), \
             mock.patch.object(qualification.component_build, "extract_metadata", return_value=self.root):
            return qualification.verify_local(receipt, content_sha256(receipt), inputs, ROOT,
                self.root / "oci", "fixture", temporary_parent=self.root)

    def test_verified_prior_execution_preserves_original_producer_and_interval(self):
        receipt, observation, inputs, record = self.envelope()
        result = self.verify_envelope(receipt, observation, inputs)
        self.assertEqual(result["mode"], "verified-prior-execution")
        self.assertEqual(result["qualification"], record)
        self.assertEqual(result["qualification"]["producer"], receipt["contract"]["producer"])

    def test_even_byte_trusted_receipt_cannot_assert_incomplete_or_unknown_qualification(self):
        for change in ({"coverage": []}, {"mode": "cached"}, {"schema_version": True}, {"extra": "unknown"}):
            receipt, observation, inputs, _ = self.envelope(change)
            with self.subTest(change=change), self.assertRaises(IdentityError):
                self.verify_envelope(receipt, observation, inputs)

    def test_missing_qualification_metadata_is_rejected_before_extracting(self):
        receipt, observation, inputs, _ = self.envelope()
        receipt["metadata"] = [record for record in receipt["metadata"] if record["path"] != qualification.RECORD_PATH]
        with self.assertRaisesRegex(IdentityError, "evidence set differs"):
            self.verify_envelope(receipt, observation, inputs)
        self.summary.write_text("PASS: fixture\n")
        (self.directory / "gcc.execute.make.log").write_text("different log")
        with self.assertRaises(IdentityError):
            self.check()


if __name__ == "__main__":
    unittest.main()

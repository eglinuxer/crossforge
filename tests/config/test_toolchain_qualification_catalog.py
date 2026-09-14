"""Catalog orchestration fixtures are not executed or signed GCC qualification."""

import copy
import contextlib
import io
import os
from pathlib import Path
import runpy
import shutil
import sys
import unittest
from unittest import mock

import test_component_catalog as fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import catalog_registry, component_artifacts, component_catalog, component_inputs
    from crossforge_internal import ci_toolchain_qualification, component_ci, component_retry
    from crossforge_internal import component_qualification, qualification_execution, registry_transfer
    from crossforge_internal import toolchain_qualification_handoff as handoff
    from crossforge_internal import toolchain_qualification_resolution as resolution
    from crossforge_internal.identity import IdentityError, canonical_bytes, content_sha256, load_json
    CATALOG_CLI = runpy.run_path(str(ROOT / "scripts/component-catalog.py"))
finally:
    sys.path.pop(0)


class ToolchainQualificationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ComponentCatalogTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.producer = self.fixture.root, self.fixture.producer
        self.execution = {"build": {"fixture": "build settings"}, "host": {"fixture": "physical host"}}
        self.signing = {"workflow": component_catalog.TOOLCHAIN_QUALIFICATION_WORKFLOW, "event": "push"}
        self.output = self.root / "output"
        locked = self.root / ".github/locked-tools"
        locked.mkdir(parents=True)
        shutil.copyfile(ROOT / ".github/locked-tools/oras.json", locked / "oras.json")
        self.make_entry("x86_64", "gcc-full")

    def make_entry(self, arch, profile):
        self.settings = component_qualification.spec(arch, profile)
        copies = component_qualification.report_copies(ROOT, self.settings)
        parameters = {"execution": self.execution, "qualification": self.settings, "report_copies": copies}
        self.expected = component_inputs.capture(self.root, self.settings["component"], "qualification",
            ["recipe"], [self.settings["triple"]], parameters=parameters)
        contract = component_artifacts.contract("qualification", self.expected, self.producer)
        paths = sorted([component_artifacts.CONTRACT_PATH, component_qualification.RECORD_PATH,
                        component_qualification.PROGRESS_PATH] + list(copies.values()))
        for name in paths:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture report; not real qualification")
        (self.root / component_artifacts.CONTRACT_PATH).write_bytes(canonical_bytes(contract))
        observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1,
            "platform": "linux/amd64", "root_digest": "sha256:" + "1" * 64,
            "platform_digest": "sha256:" + "2" * 64, "config_digest": "sha256:" + "3" * 64}
        receipt = component_artifacts.receipt(contract, observation, self.root, paths)
        self.entry = {"reference": component_catalog.REPOSITORY + "@" + observation["root_digest"],
            "receipt_sha256": content_sha256(receipt), "receipt": receipt}
        return self.entry

    def document(self):
        return handoff.document(ROOT, self.settings["arch"], self.settings["profile"],
            self.producer, self.execution, self.entry)

    def test_supported_gate_catalogs_keep_original_source_and_exact_signer(self):
        for arch, profile in (("x86_64", "toolchain"), ("aarch64", "toolchain"),
                              ("x86_64", "gcc-smoke"), ("aarch64", "gcc-smoke"), ("x86_64", "gcc-full")):
            self.make_entry(arch, profile)
            for event in ("push", "workflow_dispatch"):
                with self.subTest(arch=arch, profile=profile, event=event):
                    signing = dict(self.signing, event=event)
                    value = component_catalog.document(self.producer, [self.entry], signing)
                    self.assertEqual(value["schema_version"], 5)
                    self.fixture.write_catalog(value)
                    with mock.patch.object(component_catalog.subprocess, "run") as verify:
                        selected = component_catalog.select(self.root, self.fixture.path, self.fixture.bundle,
                            self.fixture.cosign, self.expected, "qualification", self.root)
                    command = verify.call_args[0][0]
                    self.assertEqual(command[command.index("--certificate-identity") + 1],
                        "https://github.com/eglinuxer/crossforge/" + signing["workflow"] + "@refs/heads/main")
                    self.assertEqual(command[command.index("--certificate-github-workflow-sha") + 1], self.producer["source_commit"])
                    self.assertEqual(command[command.index("--certificate-github-workflow-trigger") + 1], event)
                    self.assertEqual(selected["entry"], self.entry)
                    self.assertEqual(selected["authentication"]["producer"], self.producer)

    def test_other_signers_cannot_authorize_toolchain_qualification(self):
        for workflow in (component_catalog.MAIN_WORKFLOW, component_catalog.PYTHON_WORKFLOW,
                         component_catalog.PYTHON_ROW_WORKFLOW):
            with self.subTest(workflow=workflow), self.assertRaises(IdentityError):
                component_catalog.document(self.producer, [self.entry], dict(self.signing, workflow=workflow))
        with self.assertRaises(IdentityError):
            component_catalog.document(self.producer, [self.fixture.entry], self.signing)

    def test_schema_profile_target_and_evidence_confusion_are_rejected(self):
        valid = component_catalog.document(self.producer, [self.entry], self.signing)
        for mutation in ("version", "arm-full", "settings", "target", "component", "reports", "metadata", "signer", "event"):
            value = copy.deepcopy(valid)
            entry = value["entries"][0]
            inputs = entry["receipt"]["contract"]["inputs"]
            if mutation == "version": value["schema_version"] = True
            elif mutation == "arm-full": inputs["parameters"]["qualification"]["arch"] = "aarch64"
            elif mutation == "settings": inputs["parameters"]["qualification"]["stages"] = []
            elif mutation == "target": inputs["targets"] = ["aarch64-unknown-linux-gnu"]
            elif mutation == "component": inputs["component"] = "qualification/python-cp39"
            elif mutation == "reports": inputs["parameters"]["report_copies"] = {}
            elif mutation == "metadata": entry["receipt"]["metadata"].pop()
            elif mutation == "signer": value["signing"]["workflow"] = component_catalog.MAIN_WORKFLOW
            elif mutation == "event": value["signing"]["event"] = "pull_request"
            entry["receipt_sha256"] = content_sha256(entry["receipt"])
            with self.subTest(mutation=mutation), self.assertRaises(IdentityError):
                component_catalog.validate(value)

    def test_handoff_binds_complete_current_policy_and_original_run(self):
        value = self.document()
        self.assertEqual(handoff.verify(ROOT, value, content_sha256(value), self.producer["source_commit"],
            self.producer["invocation"]), value)
        for field in ("source_commit", "invocation"):
            changed = copy.deepcopy(value)
            changed["producer"][field] = "b" * 40 if field == "source_commit" else self.producer[field].replace("/attempts/2", "/attempts/3")
            with self.subTest(field=field), self.assertRaises(IdentityError):
                handoff.validate(ROOT, changed)
        with self.assertRaises(IdentityError):
            handoff.verify(ROOT, value, "f" * 64, self.producer["source_commit"], self.producer["invocation"])
        changed = copy.deepcopy(value)
        changed["entry"]["receipt"]["contract"]["inputs"]["parameters"]["report_copies"].popitem()
        changed["entry"]["receipt_sha256"] = content_sha256(changed["entry"]["receipt"])
        with self.assertRaisesRegex(IdentityError, "report policy"):
            handoff.validate(ROOT, changed)

    def patch(self, module, name, **kwargs):
        patcher = mock.patch.object(module, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setup_resolution(self, missing=False):
        self.environment = self.patch(qualification_execution, "execution_identity", return_value=self.execution)
        self.bind = self.patch(component_qualification, "bind_subjects", return_value=({"bound": "graph"}, {"bound": "artifacts"}))
        self.capture = self.patch(component_qualification, "qualification_inputs", return_value=self.expected)
        selected = {"status": "authenticated-reference", "entry": self.entry,
            "catalog": {"reference": component_catalog.REPOSITORY + "@sha256:" + "d" * 64},
            "authentication": {"fixture": "upstream verified"}}
        if missing: selected = {"status": "missing", "reason": "missing-input-index", "input_tag": "fixture"}
        self.lookup = self.patch(catalog_registry, "lookup", return_value=selected)
        self.fetch = self.patch(registry_transfer, "fetch")
        self.verify = self.patch(component_qualification, "verify_local", return_value={
            "mode": "verified-prior-execution", "qualification": {"producer": self.producer, "fixture": "prior reports"}})
        self.produce = self.patch(component_qualification, "produce")

    def resolve(self, reference=None):
        return resolution.resolve(self.root, {"original": "graph"}, "x86_64", "gcc-full", self.execution,
            {"fixture": "trusted raw subjects"}, self.fixture.cosign, self.output, "builder", "/oras",
            catalog_reference=reference)

    def test_resolution_verifies_artifact_and_prior_execution_without_relabelling(self):
        self.setup_resolution()
        result = self.resolve()
        self.assertEqual(result["status"], "verified-qualification")
        self.assertEqual(result["producer"], self.producer)
        self.assertEqual(result["verification"], self.verify.return_value)
        self.assertEqual(load_json(result["subject"]["receipt"]), self.entry["receipt"])
        self.fetch.assert_called_once()
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args[0][:3], (self.entry["receipt"], self.entry["receipt_sha256"], self.expected))
        self.assertEqual(self.capture.call_count, 2)
        self.produce.assert_not_called()

    def test_only_missing_index_requests_new_qualification(self):
        self.setup_resolution(missing=True)
        self.assertEqual(self.resolve()["status"], "qualification-required")
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.produce.assert_not_called()
        shutil.rmtree(self.output)
        with self.assertRaisesRegex(IdentityError, "fixed toolchain"):
            self.resolve(component_catalog.REPOSITORY + "@sha256:" + "d" * 64)

    def test_signature_artifact_and_domain_failures_never_become_misses(self):
        self.setup_resolution()
        for boundary in (self.bind, self.lookup, self.fetch, self.verify):
            boundary.side_effect = IdentityError("fixture rejection")
            with self.subTest(boundary=boundary), self.assertRaises(IdentityError):
                self.resolve()
            self.assertFalse((self.output / "resolution.json").exists())
            boundary.side_effect = None
            if self.output.exists(): shutil.rmtree(self.output)
        self.produce.assert_not_called()

    def test_changed_source_or_environment_prevents_success_record(self):
        self.setup_resolution()
        changed = copy.deepcopy(self.expected)
        changed["parameters"]["execution"]["host"]["fixture"] = "changed host"
        self.capture.side_effect = [self.expected, changed]
        with self.assertRaises(IdentityError): self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())
        shutil.rmtree(self.output)
        self.capture.side_effect = None
        self.environment.side_effect = [self.execution, {}]
        with self.assertRaisesRegex(IdentityError, "environment changed"): self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())

    def test_only_dedicated_cli_signs_the_exact_original_gate_handoff(self):
        value = self.document()
        path = self.root / "handoff.json"
        path.write_bytes(canonical_bytes(value))
        for mode in (None, "--main-ci", "--python-ci", "--python-row-ci", "--toolchain-qualification-ci"):
            output = self.root / (str(mode) + ".json")
            args = ["from-handoff", "--handoff", str(path), "--handoff-sha256", content_sha256(value),
                    "--output", str(output)] + ([mode] if mode else [])
            with mock.patch.object(component_ci, "checked_source", return_value=self.producer), \
                 mock.patch.dict(os.environ, GITHUB_EVENT_NAME="push"), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = CATALOG_CLI["main"](args)
            with self.subTest(mode=mode):
                self.assertEqual(status == 0, mode == "--toolchain-qualification-ci")
                self.assertEqual(output.exists(), status == 0)
                if status == 0:
                    catalog = load_json(output)
                    self.assertEqual(catalog["schema_version"], 5)
                    self.assertEqual(catalog["entries"], [self.entry])
                    directory = self.root / "signed"
                    directory.mkdir()
                    shutil.copyfile(output, directory / "catalog.json")
                    (directory / "catalog.sigstore.json").write_text("fixture signature bytes")
                    (directory / "authentication.json").write_text("fixture authentication bytes")
                    metadata = component_retry.catalog_metadata(directory, self.producer, self.producer["invocation"])
                    self.assertEqual(metadata["producer-invocation"], self.producer["invocation"])

    def setup_producer(self, missing=False):
        self.source_check = self.patch(component_ci, "checked_source", return_value=self.producer)
        self.patch(component_ci, "source_graph", return_value={"fixture": "source graph"})
        self.patch(qualification_execution, "execution_identity", return_value=self.execution)
        self.raw = self.patch(ci_toolchain_qualification.component_resolution, "toolchain", side_effect=lambda *args: {
            "status": "verified-build-component", "subject": {"fixture": args[3]}})
        self.resolve_gate = self.patch(resolution, "resolve", return_value={
            "status": "qualification-required" if missing else "verified-qualification",
            "inputs_sha256": component_inputs.identity(self.expected)})
        self.built = {"artifact": self.entry["receipt"]["artifact"], "receipt_sha256": self.entry["receipt_sha256"],
                      "qualification": {"fixture": "producer execution; not real qualification"}}
        def produce(*args):
            path = args[7] / "receipt.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(canonical_bytes(self.entry["receipt"]))
            return self.built
        self.build_gate = self.patch(component_qualification, "produce", side_effect=produce)
        self.publish_gate = self.patch(registry_transfer, "publish", return_value={"reference": self.entry["reference"]})

    def ensure(self):
        return ci_toolchain_qualification.ensure(ROOT, "x86_64", "gcc-full", self.output,
            "builder", "/oras", self.fixture.cosign)

    def test_reused_gate_neither_reexecutes_nor_republishes(self):
        self.setup_producer()
        result = self.ensure()
        self.assertFalse(result["produced"])
        self.assertEqual(set(result["subjects"]), {"toolchain-install", "gcc-test-context"})
        self.build_gate.assert_not_called()
        self.publish_gate.assert_not_called()

    def test_new_gate_handoff_is_emitted_only_after_verified_raw_roles_and_publication(self):
        self.setup_producer(missing=True)
        # Handoff policy validation uses the real current report contract;
        # compilation and transport remain explicitly mocked boundaries.
        result = self.ensure()
        self.assertTrue(result["produced"])
        self.assertEqual(result["producer_invocation"], self.producer["invocation"])
        self.assertEqual(load_json(result["handoff"])["entry"], self.entry)
        self.build_gate.assert_called_once()
        self.publish_gate.assert_called_once()

    def test_missing_raw_role_stops_before_qualification_resolution(self):
        self.setup_producer()
        self.raw.side_effect = None
        self.raw.return_value = {"status": "build-required"}
        with self.assertRaisesRegex(IdentityError, "prepared raw role"):
            self.ensure()
        self.resolve_gate.assert_not_called()
        self.build_gate.assert_not_called()
        self.publish_gate.assert_not_called()

    def test_producer_source_change_blocks_publication_and_success_handoff(self):
        self.setup_producer(missing=True)
        self.source_check.side_effect = [self.producer, dict(self.producer, source_commit="b" * 40)]
        with self.assertRaisesRegex(IdentityError, "source or invocation changed"):
            self.ensure()
        self.publish_gate.assert_not_called()
        self.assertFalse((self.output / "handoff.json").exists())
        self.assertFalse((self.output / "report/result.json").exists())


if __name__ == "__main__":
    unittest.main()

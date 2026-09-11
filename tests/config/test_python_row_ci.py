"""Qualified row CI fixtures do not simulate successful target execution or signing."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_component_catalog as catalog_fixtures
import test_ci_component_routing as workflow_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import catalog_registry, ci_python_rows, component_artifacts, component_build
    from crossforge_internal import component_catalog, component_ci, component_inputs, component_resolution, component_retry
    from crossforge_internal import python_components, python_handoff, python_qualification, python_row_handoff
    from crossforge_internal import python_row_resolution, qualification_execution, registry_transfer
    from crossforge_internal.identity import IdentityError, canonical_bytes, content_sha256, load_json
    CATALOG_CLI = runpy.run_path(str(ROOT / "scripts/component-catalog.py"))
    ROW_CLI = runpy.run_path(str(ROOT / "scripts/ci-python-row.py"))
finally:
    sys.path.pop(0)


class PythonRowCITests(unittest.TestCase):
    def setUp(self):
        self.fixture = catalog_fixtures.ComponentCatalogTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.producer = self.fixture.producer
        self.execution = {"build": {"fixture": "build environment"}, "host": {"fixture": "physical host"}}
        self.settings = python_qualification.spec(ROOT, "cp39")
        self.graph, self.bindings = {"fixture": "bound row graph"}, {"fixture": "seven verified subjects"}
        self.subjects = {name: {"fixture": name} for name in
            ["x86_64-toolchain", "aarch64-toolchain"] + [name for name, _, _ in python_handoff.PARTS]}
        parameters = {"execution": self.execution, "qualification": self.settings,
                      "recipes": {self.settings["target"]: {"frontend": "dockerfile@sha256:" + "f" * 64}}}
        self.expected = component_inputs.capture(self.root, self.settings["component"], "qualification", ["recipe"],
            self.settings["targets"], parameters=parameters)
        contract = component_artifacts.contract("qualification", self.expected, self.producer)
        for name in python_qualification.metadata_paths():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture metadata; not an executed qualification")
        (self.root / component_artifacts.CONTRACT_PATH).write_bytes(canonical_bytes(contract))
        observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
            "root_digest": "sha256:" + "1" * 64, "platform_digest": "sha256:" + "2" * 64, "config_digest": "sha256:" + "3" * 64}
        receipt = component_artifacts.receipt(contract, observation, self.root, python_qualification.metadata_paths())
        self.entry = {"reference": component_catalog.REPOSITORY + "@" + observation["root_digest"],
                      "receipt_sha256": content_sha256(receipt), "receipt": receipt}
        self.output = self.root / "output"
        self.signing = {"workflow": component_catalog.PYTHON_ROW_WORKFLOW, "event": "push"}
        locked = self.root / ".github/locked-tools"
        locked.mkdir(parents=True)
        shutil.copyfile(str(ROOT / ".github/locked-tools/oras.json"), str(locked / "oras.json"))

    def patch(self, module, name, **kwargs):
        patcher = mock.patch.object(module, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def handoff(self):
        return python_row_handoff.document(ROOT, "cp39", self.producer, self.execution, self.entry)

    def test_handoff_keeps_exact_original_producer_environment_and_complete_evidence(self):
        value = self.handoff()
        verified = python_row_handoff.verify(ROOT, value, content_sha256(value),
            self.producer["source_commit"], self.producer["invocation"])
        self.assertEqual(verified, value)
        for change in ("row", "schema", "extra", "environment", "producer", "metadata", "reference", "settings", "target", "role"):
            value = self.handoff()
            entry = value["components"]["cp39"]
            contract = entry["receipt"]["contract"]
            if change == "row":
                value["row"] = "cp310"
            elif change == "schema":
                value["schema_version"] = True
            elif change == "extra":
                value["trusted"] = True
            elif change == "environment":
                value["execution"]["host"] = {"fixture": "different host"}
            elif change == "producer":
                contract["producer"]["invocation"] = self.producer["invocation"].replace("123/", "999/")
            elif change == "metadata":
                entry["receipt"]["metadata"].pop()
            elif change == "reference":
                entry["reference"] = component_catalog.REPOSITORY + "@sha256:" + "0" * 64
            elif change == "settings":
                contract["inputs"]["parameters"]["qualification"]["version"] = "3.9.0"
            elif change == "target":
                contract["inputs"]["targets"] = ["x86_64-unknown-linux-gnu"]
            else:
                contract["role"] = "python-install"
            entry["receipt_sha256"] = content_sha256(entry["receipt"])
            with self.subTest(change=change), self.assertRaises((IdentityError, ValueError)):
                python_row_handoff.verify(ROOT, value, content_sha256(value),
                    self.producer["source_commit"], self.producer["invocation"])

    def test_handoff_requires_independent_digest_and_exact_source_run_attempt(self):
        value = self.handoff()
        for digest, commit, invocation in (("0" * 64, self.producer["source_commit"], self.producer["invocation"]),
                (content_sha256(value), "b" * 40, self.producer["invocation"]),
                (content_sha256(value), self.producer["source_commit"], self.producer["invocation"] + "0")):
            with self.assertRaises(IdentityError):
                python_row_handoff.verify(ROOT, value, digest, commit, invocation)
        for execution in ({"build": {}}, {"build": self.execution["build"], "host": {}},
                          {"build": self.execution["build"], "host": "physical host"}):
            with self.assertRaises(IdentityError):
                python_row_handoff.document(ROOT, "cp39", self.producer, execution, self.entry)

    def test_catalog_has_separate_qualification_signer_and_no_raw_authority_escalation(self):
        value = component_catalog.document(self.producer, [self.entry], self.signing)
        self.assertEqual(value["schema_version"], 4)
        self.assertEqual(component_catalog.validate(value), value)
        for signer in (component_catalog.MAIN_WORKFLOW, component_catalog.PYTHON_WORKFLOW):
            with self.assertRaises(IdentityError):
                component_catalog.document(self.producer, [self.entry], dict(self.signing, workflow=signer))
        with self.assertRaises(IdentityError):
            component_catalog.document(self.producer, [self.fixture.entry], self.signing)
        for change in ("signer", "version", "metadata", "settings", "scope", "event"):
            mutated = copy.deepcopy(value)
            if change == "signer":
                mutated["signing"]["workflow"] = component_catalog.PYTHON_WORKFLOW
            elif change == "version":
                mutated["schema_version"] = 4.0
            elif change == "event":
                mutated["signing"]["event"] = "pull_request"
            else:
                entry = mutated["entries"][0]
                if change == "metadata":
                    entry["receipt"]["metadata"].pop()
                elif change == "settings":
                    entry["receipt"]["contract"]["inputs"]["parameters"]["qualification"] = []
                else:
                    entry["receipt"]["contract"]["inputs"]["scope"] = "build"
                entry["receipt_sha256"] = content_sha256(entry["receipt"])
            with self.subTest(change=change), self.assertRaises(IdentityError):
                component_catalog.validate(mutated)

    def test_catalog_verifier_receives_exact_row_signer_original_source_and_event(self):
        for event in ("push", "workflow_dispatch"):
            value = component_catalog.document(self.producer, [self.entry], dict(self.signing, event=event))
            self.fixture.write_catalog(value)
            with mock.patch.object(component_catalog.subprocess, "run") as verify:
                result = component_catalog.select(self.root, self.fixture.path, self.fixture.bundle, self.fixture.cosign,
                    self.expected, "qualification")
            command = verify.call_args[0][0]
            for flag, expected in {"--certificate-identity": "https://github.com/eglinuxer/crossforge/" +
                    component_catalog.PYTHON_ROW_WORKFLOW + "@refs/heads/main",
                    "--certificate-github-workflow-sha": self.producer["source_commit"],
                    "--certificate-github-workflow-trigger": event}.items():
                self.assertEqual(command[command.index(flag) + 1], expected)
            self.assertEqual(result["entry"], self.entry)
            self.assertNotIn("qualification", result)

    def test_signing_cli_and_retry_preserve_original_row_producer(self):
        value = self.handoff()
        path = self.root / "handoff.json"
        component_build.write_json(path, value)
        current = dict(self.producer, invocation=self.producer["invocation"].replace("attempts/2", "attempts/3"))
        for mode in ("--python-row-ci", "--python-ci", "--main-ci", None):
            directory = self.root / (mode or "pilot")
            directory.mkdir()
            output = directory / "catalog.json"
            args = ["from-handoff", "--handoff", str(path), "--handoff-sha256", content_sha256(value),
                    "--producer-invocation", self.producer["invocation"], "--output", str(output)]
            if mode:
                args.append(mode)
            with mock.patch.object(component_ci, "checked_source", return_value=current), \
                 mock.patch.dict(os.environ, GITHUB_EVENT_NAME="push"), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = CATALOG_CLI["main"](args)
            self.assertEqual(status == 0, mode == "--python-row-ci")
            self.assertEqual(output.exists(), mode == "--python-row-ci")
            if output.exists():
                self.assertEqual(load_json(output)["producer"], self.producer)
                (directory / "catalog.sigstore.json").write_text("fixture bundle")
                (directory / "authentication.json").write_text("fixture authentication")
                metadata = component_retry.catalog_metadata(directory, current, self.producer["invocation"])
                self.assertEqual(metadata["producer-invocation"], self.producer["invocation"])
                (directory / "catalog.json").write_bytes(output.read_bytes() + b" ")
                with self.assertRaises(IdentityError):
                    component_retry.catalog_metadata(directory, current, self.producer["invocation"])

    def setup_resolution(self, selection=None):
        self.selected = selection or {"status": "authenticated-reference", "entry": self.entry,
            "catalog": {"reference": component_catalog.REPOSITORY + "@sha256:" + "d" * 64},
            "authentication": {"fixture": "upstream verified"}}
        self.environment = self.patch(qualification_execution, "execution_identity", return_value=self.execution)
        self.patch(python_qualification, "spec", return_value=self.settings)
        self.bind = self.patch(python_components, "bind_row", return_value=(self.graph, self.bindings))
        self.capture = self.patch(python_qualification, "inputs", return_value=self.expected)
        self.lookup = self.patch(catalog_registry, "lookup", return_value=self.selected)
        self.fetch = self.patch(registry_transfer, "fetch")
        self.verified = {"mode": "verified-prior-execution", "receipt_sha256": self.entry["receipt_sha256"],
                         "reference": "oci-layout://fixture", "qualification": {"fixture": "prior verified execution"}}
        self.verify = self.patch(python_qualification, "verify_local", return_value=self.verified)

    def resolve(self, reference=None):
        return python_row_resolution.resolve(self.root, {"fixture": "original graph"}, "cp39", self.execution,
            self.subjects, self.root / "cosign", self.output, "builder", self.root / "oras", self.root / "docker", reference)

    def test_resolution_authenticates_seven_subjects_then_signature_then_actual_execution(self):
        self.setup_resolution()
        events = []
        self.bind.side_effect = lambda *args: events.append("subjects") or (self.graph, self.bindings)
        self.lookup.side_effect = lambda *args, **kwargs: events.append("catalog") or self.selected
        self.fetch.side_effect = lambda *args, **kwargs: events.append("OCI bytes")
        self.verify.side_effect = lambda *args, **kwargs: events.append("execution and files") or self.verified
        result = self.resolve()
        self.assertEqual(events, ["subjects", "catalog", "OCI bytes", "execution and files"])
        self.assertEqual(result["status"], "verified-qualified-row")
        self.assertEqual(result["producer"], self.producer)
        self.assertEqual(result["verification"], self.verified)
        self.assertEqual(self.bind.call_args[0][4], self.subjects)
        self.assertEqual(self.verify.call_args[0][:3], (self.entry["receipt"], self.entry["receipt_sha256"], self.expected))
        self.assertEqual(self.lookup.call_args[0][2], "qualification")
        self.assertEqual(self.lookup.call_args[0][8], self.root / "docker/config.json")
        self.assertEqual(load_json(self.output / "receipt.json"), self.entry["receipt"])
        self.assertEqual(self.capture.call_count, 2)

    def test_missing_row_requests_qualification_without_building_or_accepting_raw_bytes(self):
        self.setup_resolution({"status": "missing", "reason": "catalog-index-absent", "input_tag": "input-fixture"})
        result = self.resolve()
        self.assertEqual(result["status"], "qualification-required")
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.assertNotIn("subject", result)

    def test_fixed_recovery_miss_and_failed_signature_transport_or_execution_never_fall_back(self):
        self.setup_resolution()
        for boundary in (self.bind, self.lookup, self.fetch, self.verify):
            with self.subTest(boundary=boundary):
                boundary.side_effect = IdentityError("fixture rejection")
                with self.assertRaises(IdentityError):
                    self.resolve()
                self.assertFalse((self.output / "resolution.json").exists())
                boundary.side_effect = None
                if self.output.exists():
                    shutil.rmtree(str(self.output))
        self.lookup.return_value = {"status": "missing", "reason": "fixture", "input_tag": "fixture"}
        with self.assertRaisesRegex(IdentityError, "fixed row recovery"):
            self.resolve(component_catalog.REPOSITORY + "@sha256:" + "d" * 64)

    def test_resolution_rejects_environment_input_changes_and_existing_output(self):
        self.setup_resolution()
        self.environment.return_value = dict(self.execution, host={"fixture": "other host"})
        with self.assertRaisesRegex(IdentityError, "environment differs"):
            self.resolve()
        self.lookup.assert_not_called()
        self.environment.side_effect = [self.execution, dict(self.execution, host={"fixture": "changed host"})]
        with self.assertRaisesRegex(IdentityError, "environment changed"):
            self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())
        shutil.rmtree(str(self.output))
        self.environment.side_effect = None
        self.environment.return_value = self.execution
        changed = copy.deepcopy(self.expected)
        changed["parameters"]["inspection"] = "changed inspection binary"
        self.capture.side_effect = [self.expected, changed]
        with self.assertRaises(IdentityError):
            self.resolve()
        self.assertFalse((self.output / "resolution.json").exists())
        with self.assertRaisesRegex(IdentityError, "directory must be new"):
            self.resolve()

    def setup_ensure(self, missing=False):
        self.patch(component_ci, "checked_source", return_value=self.producer)
        self.patch(component_ci, "source_graph", return_value=self.graph)
        self.patch(ci_python_rows.python_row_install, "validate_graph", return_value=("python-cp39-dev", self.settings))
        self.install = self.patch(ci_python_rows.python_row_install, "execute", return_value={"fixture": "independent installation"})
        self.patch(qualification_execution, "execution_identity", return_value=self.execution)
        self.patch(ci_python_rows, "prepared_subjects", return_value=(self.subjects, {"fixture": "raw resolutions"}))
        resolution = {"status": "qualification-required" if missing else "verified-qualified-row",
                      "inputs_sha256": component_inputs.identity(self.expected),
                      "subject": {"receipt": "prior.json", "receipt_sha256": self.entry["receipt_sha256"], "layout": "prior-oci"}}
        self.resolver = self.patch(python_row_resolution, "resolve", return_value=resolution)
        def produce(*args):
            component_build.write_json(args[6] / "receipt.json", self.entry["receipt"])
            return {"artifact": self.entry["receipt"]["artifact"], "receipt_sha256": self.entry["receipt_sha256"],
                    "qualification": {"fixture": "fresh production"}}
        self.produce = self.patch(python_qualification, "produce", side_effect=produce)
        self.publish = self.patch(registry_transfer, "publish", return_value={"reference": self.entry["reference"]})

    def ensure(self):
        return ci_python_rows.ensure(ROOT, "cp39", self.output, "builder", self.root / "oras", self.root / "cosign")

    def test_previously_verified_row_never_becomes_a_new_producer_or_signed_handoff(self):
        self.setup_ensure()
        result = self.ensure()
        self.assertFalse(result["produced"])
        self.assertEqual(result["subjects"], self.subjects)
        self.assertFalse((self.output / "handoff.json").exists())
        self.produce.assert_not_called()
        self.publish.assert_not_called()
        self.install.assert_called_once()
        self.assertEqual(self.install.call_args[0][5], self.resolver.return_value["subject"])
        self.assertEqual(result["installation"], {"fixture": "independent installation"})

    def test_missing_row_runs_formal_producer_then_publishes_only_its_qualified_receipt(self):
        self.setup_ensure(missing=True)
        events = []
        self.install.side_effect = lambda *args: events.append("installation") or {"fixture": "installation"}
        self.publish.side_effect = lambda *args: events.append("publication") or {"reference": self.entry["reference"]}
        result = self.ensure()
        self.assertEqual(events, ["installation", "publication"])
        self.assertTrue(result["produced"])
        self.assertEqual(self.produce.call_args[0][:6], (ROOT, self.graph, "cp39", self.execution, self.producer, self.subjects))
        self.assertEqual(self.publish.call_args[0][1], self.entry["receipt"]["artifact"]["root_digest"])
        value = load_json(self.output / "handoff.json")
        python_row_handoff.verify(ROOT, value, result["handoff_sha256"], self.producer["source_commit"], self.producer["invocation"])

    def test_failed_independent_installation_blocks_fresh_publication_and_handoff(self):
        self.setup_ensure(missing=True)
        self.install.side_effect = IdentityError("fixture independent append failed")
        with self.assertRaisesRegex(IdentityError, "independent append failed"):
            self.ensure()
        self.publish.assert_not_called()
        self.assertFalse((self.output / "handoff.json").exists())
        self.assertFalse((self.output / "report/result.json").exists())

    def test_reused_row_still_fails_the_job_when_independent_installation_fails(self):
        self.setup_ensure()
        self.install.side_effect = IdentityError("fixture independent append failed")
        with self.assertRaisesRegex(IdentityError, "independent append failed"):
            self.ensure()
        self.produce.assert_not_called()
        self.publish.assert_not_called()
        self.assertFalse((self.output / "report/result.json").exists())

    def test_failed_execution_or_changed_input_cannot_publish_success(self):
        self.setup_ensure(missing=True)
        self.produce.side_effect = subprocess.CalledProcessError(1, "fixture failed qualification")
        with self.assertRaises(subprocess.CalledProcessError):
            self.ensure()
        self.publish.assert_not_called()
        self.assertFalse((self.output / "report/result.json").exists())
        self.produce.side_effect = lambda *args: component_build.write_json(args[6] / "receipt.json", self.entry["receipt"]) or {
            "artifact": self.entry["receipt"]["artifact"], "receipt_sha256": self.entry["receipt_sha256"]}
        self.resolver.return_value["inputs_sha256"] = "0" * 64
        with self.assertRaisesRegex(IdentityError, "inputs changed"):
            self.ensure()
        self.publish.assert_not_called()

    def test_failed_producer_preserves_progress_without_installed_payload(self):
        self.setup_ensure(missing=True)
        def fail(*args):
            output = args[6]
            component_build.write_json(output / "inputs.json", self.expected)
            progress = output / "payload" / python_qualification.PROGRESS_PATH
            progress.parent.mkdir(parents=True)
            progress.write_text('fixture failed execution\n')
            installed = output / "payload/opt/crossforge/python/cp39/large-installed-file"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture installed payload")
            raise subprocess.CalledProcessError(1, "fixture failed qualification")
        self.produce.side_effect = fail
        with self.assertRaises(subprocess.CalledProcessError):
            self.ensure()
        report = self.output / "report/qualification"
        self.assertEqual((report / "payload" / python_qualification.PROGRESS_PATH).read_text(), 'fixture failed execution\n')
        self.assertEqual(load_json(report / "inputs.json"), self.expected)
        self.assertFalse((report / "payload/opt/crossforge/python").exists())
        self.assertFalse((self.output / "handoff.json").exists())
        self.publish.assert_not_called()

    def test_changed_producer_source_or_host_cannot_reach_publication(self):
        self.setup_ensure(missing=True)
        with mock.patch.object(component_ci, "checked_source", side_effect=[self.producer, dict(self.producer, source_commit="b" * 40)]):
            with self.assertRaisesRegex(IdentityError, "source or invocation changed"):
                self.ensure()
        self.publish.assert_not_called()
        shutil.rmtree(str(self.output))
        with mock.patch.object(qualification_execution, "execution_identity", side_effect=[self.execution,
                dict(self.execution, host={"fixture": "changed host"})]):
            with self.assertRaisesRegex(IdentityError, "environment changed"):
                self.ensure()
        self.publish.assert_not_called()

    def test_diagnostics_reject_symlinked_logs_before_copying(self):
        source, destination = self.root / "diagnostic-source", self.root / "diagnostic-output"
        progress = source / "payload" / python_qualification.PROGRESS_PATH
        progress.parent.mkdir(parents=True)
        external = self.root / "outside-log"
        external.write_text("fixture outside allowed diagnostic scope")
        progress.symlink_to(external)
        with self.assertRaisesRegex(IdentityError, "symlink"):
            ci_python_rows.preserve_qualification(source, destination, "cp39")
        self.assertFalse(destination.exists())

    def test_prepared_subjects_keeps_dependency_order_and_rejects_any_raw_miss(self):
        observed = []
        def toolchain(source, graph, arch, role, *args):
            name = arch + "-toolchain"
            observed.append(name)
            return {"status": "verified-build-component", "subject": self.subjects[name]}
        def python(source, graph, row, arch, kind, execution, dependencies, *args):
            name = "build" if arch == "build" else arch + "-" + kind
            observed.append(name)
            self.assertEqual(dependencies, {} if arch == "build" else {
                "build-python": self.subjects["build"], "toolchain-install": self.subjects[arch + "-toolchain"]})
            return {"status": "verified-build-component", "subject": self.subjects[name]}
        self.patch(component_resolution, "toolchain", side_effect=toolchain)
        raw = self.patch(component_resolution, "python", side_effect=python)
        def acquire():
            return ci_python_rows.prepared_subjects(ROOT, self.graph, "cp39", self.execution,
                self.root / "subjects", self.root / "report", "builder", self.root / "oras", self.root / "cosign", None)
        subjects, results = acquire()
        self.assertEqual(subjects, self.subjects)
        self.assertEqual(observed, ["x86_64-toolchain", "aarch64-toolchain"] + [name for name, _, _ in python_handoff.PARTS])
        self.assertEqual(set(results), set(subjects))
        raw.side_effect = None
        raw.return_value = {"status": "build-required"}
        with self.assertRaisesRegex(IdentityError, "prepared raw part"):
            acquire()

    def test_workflow_separates_row_producer_signer_store_and_exact_artifact_retry(self):
        workflow = (ROOT / ".github/workflows/produce-python-row.yml").read_text()
        job = workflow_fixtures.job
        ensure, sign, store = [job(workflow, name) for name in ("ensure", "sign", "store")]
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn('ci-python-row.py --row "$PYTHON_ROW"', ensure)
        self.assertLess(ensure.index('checked_source(Path.cwd(), "main")'), ensure.index('docker login'))
        self.assertIn("packages: write", ensure)
        self.assertNotIn("id-token:", ensure)
        self.assertIn("id-token: write", sign)
        self.assertNotIn("packages:", sign)
        self.assertIn("from-handoff --python-row-ci", sign)
        self.assertIn("needs.ensure.outputs.handoff-sha256", sign)
        self.assertIn("--producer-invocation", sign)
        self.assertIn("artifact-ids: ${{ needs.ensure.outputs.artifact-id }}", sign)
        self.assertIn("packages: write", store)
        self.assertNotIn("id-token:", store)
        self.assertLess(store.index("verify-catalog"), store.index("docker login"))
        self.assertIn("component-catalog.py publish", store)
        self.assertIn("check-production", job(workflow, "verified"))

    def test_cli_passes_arguments_and_reports_domain_failure(self):
        args = ["--row", "cp39", "--builder", "builder", "--output", str(self.output),
                "--oras", str(self.root / "oras"), "--cosign", str(self.root / "cosign")]
        with mock.patch.object(ci_python_rows, "ensure", return_value={"produced": False}) as ensure, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ROW_CLI["main"](args), 0)
        self.assertEqual(ensure.call_args[0][1:4], ("cp39", self.output, "builder"))
        with mock.patch.object(ci_python_rows, "ensure", side_effect=IdentityError("fixture rejected")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ROW_CLI["main"](args), 1)


if __name__ == "__main__":
    unittest.main()

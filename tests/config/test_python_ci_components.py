"""Raw Python trust, dependency ordering and handoff remain separate from gates."""

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
    from crossforge_internal import catalog_registry, ci_python, component_artifacts, component_build
    from crossforge_internal import component_catalog, component_ci, component_handoff, component_inputs
    from crossforge_internal import component_resolution, python_components, python_handoff, registry_transfer
    from crossforge_internal.identity import IdentityError, content_sha256, load_json
    CATALOG_CLI = runpy.run_path(str(ROOT / "scripts/component-catalog.py"))
finally:
    sys.path.pop(0)


class PythonCIComponentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "recipe").write_text("fixture")
        (self.root / "component").mkdir()
        (self.root / ".github/locked-tools").mkdir(parents=True)
        shutil.copyfile(str(ROOT / ".github/locked-tools/oras.json"), str(self.root / ".github/locked-tools/oras.json"))
        self.producer = {"kind": "github-actions", "source_commit": "a" * 40, "source_dirty": False,
            "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/123/attempts/2", "started_at": "2026-09-10T00:00:00Z"}
        self.execution = {"buildkit_image": "image@sha256:" + "b" * 64}
        self.specs = python_handoff.specs(ROOT, "cp39")
        self.entries, self.expected = {}, {}
        for name, settings in self.specs.items():
            parameters = {"execution": self.execution, "python_component": settings,
                          "recipes": {settings["target"]: {"frontend": "dockerfile@sha256:" + "f" * 64}}}
            inputs = component_inputs.capture(self.root, settings["component"], "build", ["recipe"],
                                              settings["targets"], parameters=parameters)
            contract = component_artifacts.contract(settings["role"], inputs, self.producer)
            (self.root / component_artifacts.CONTRACT_PATH).write_text(json.dumps(contract))
            digest = "sha256:" + content_sha256(contract)
            observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
                           "root_digest": digest, "platform_digest": digest, "config_digest": "sha256:" + "c" * 64}
            receipt = component_artifacts.receipt(contract, observation, self.root, [component_artifacts.CONTRACT_PATH])
            self.expected[name] = inputs
            self.entries[name] = {"reference": component_handoff.REPOSITORY + "@" + digest,
                "receipt_sha256": content_sha256(receipt), "receipt": receipt}

    def handoff(self, names=None):
        return python_handoff.document(ROOT, "cp39", self.producer, self.execution,
            {name: self.entries[name] for name in (names or self.entries)})

    def test_handoff_accepts_only_new_parts_from_one_original_producer(self):
        for names in (["build"], ["aarch64-test-context"], list(self.entries)):
            value = self.handoff(names)
            result = python_handoff.verify(ROOT, value, content_sha256(value), self.producer["source_commit"], self.producer["invocation"])
            self.assertEqual(result["producer"], self.producer)
            self.assertEqual(set(result["components"]), set(names))

    def test_handoff_rejects_wrong_row_role_environment_schema_and_producer(self):
        for change in ("row", "role", "target", "version", "producer", "environment", "empty", "unknown", "schema", "digest"):
            value = self.handoff()
            entry = value["components"]["x86_64-install"]
            if change == "row":
                value["row"] = "cp310"
            elif change == "role":
                value["components"]["x86_64-install"] = value["components"]["x86_64-test-context"]
            elif change == "target":
                entry["receipt"]["contract"]["inputs"]["targets"] = ["aarch64-unknown-linux-gnu"]
            elif change == "version":
                entry["receipt"]["contract"]["inputs"]["parameters"]["python_component"]["version"] = "3.9.0"
            elif change == "producer":
                entry["receipt"]["contract"]["producer"]["invocation"] = self.producer["invocation"].replace("123/", "111/")
            elif change == "environment":
                value["build_execution"] = {"buildkit_image": "different"}
            elif change == "empty":
                value["components"] = {}
            elif change == "unknown":
                value["trusted"] = True
            elif change == "schema":
                value["schema_version"] = True
            else:
                entry["reference"] = component_handoff.REPOSITORY + "@sha256:" + "0" * 64
            entry["receipt_sha256"] = content_sha256(entry["receipt"])
            with self.subTest(change=change), self.assertRaises((IdentityError, ValueError)):
                python_handoff.verify(ROOT, value, content_sha256(value), self.producer["source_commit"], self.producer["invocation"])

    def test_handoff_requires_independent_hash_and_exact_source_run_attempt(self):
        value = self.handoff()
        for digest, commit, invocation in (("0" * 64, self.producer["source_commit"], self.producer["invocation"]),
                (content_sha256(value), "b" * 40, self.producer["invocation"]),
                (content_sha256(value), self.producer["source_commit"], self.producer["invocation"].replace("attempts/2", "attempts/3"))):
            with self.assertRaises(IdentityError):
                python_handoff.verify(ROOT, value, digest, commit, invocation)

    def test_raw_python_catalog_has_a_distinct_signer_policy(self):
        for event in ("push", "workflow_dispatch"):
            value = component_catalog.document(self.producer, list(self.entries.values()),
                {"workflow": component_catalog.PYTHON_WORKFLOW, "event": event})
            self.assertEqual(value["schema_version"], 3)
            self.assertEqual(component_catalog.validate(value), value)
            value["signing"]["workflow"] = component_catalog.MAIN_WORKFLOW
            with self.assertRaises(IdentityError):
                component_catalog.validate(value)

    def test_python_catalog_verifier_requires_its_exact_signer_event_and_original_commit(self):
        fixture = catalog_fixtures.ComponentCatalogTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        value = component_catalog.document(self.producer, [self.entries["build"]],
            {"workflow": component_catalog.PYTHON_WORKFLOW, "event": "push"})
        fixture.write_catalog(value)
        with mock.patch.object(component_catalog.subprocess, "run") as verify:
            result = component_catalog.select(fixture.root, fixture.path, fixture.bundle, fixture.cosign,
                self.expected["build"], "python-install")
        command = verify.call_args[0][0]
        expected = {"--certificate-identity": "https://github.com/eglinuxer/crossforge/.github/workflows/produce-python.yml@refs/heads/main",
            "--certificate-github-workflow-trigger": "push", "--certificate-github-workflow-sha": self.producer["source_commit"],
            "--certificate-github-workflow-repository": "eglinuxer/crossforge", "--certificate-github-workflow-ref": "refs/heads/main"}
        for flag, value in expected.items():
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(result["entry"], self.entries["build"])
        self.assertEqual(result["authentication"]["producer"], self.producer)

    def test_python_workflow_separates_signing_and_writing_and_requires_the_handoff_digest(self):
        workflow = (ROOT / ".github/workflows/produce-python.yml").read_text()
        job = workflow_fixtures.job
        ensure, sign, store = [job(workflow, name) for name in ("ensure", "sign", "store")]
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn('ci-python.py ensure --row "$PYTHON_ROW" --parts "$PARTS"', ensure)
        self.assertLess(ensure.index('checked_source(Path.cwd(), "main")'), ensure.index('docker login'))
        self.assertIn("packages: write", ensure)
        self.assertNotIn("id-token:", ensure)
        self.assertIn("id-token: write", sign)
        self.assertNotIn("packages:", sign)
        self.assertIn("from-handoff --python-ci", sign)
        self.assertIn("needs.ensure.outputs.handoff-sha256", sign)
        self.assertIn("artifact-ids: ${{ needs.ensure.outputs.artifact-id }}", sign)
        self.assertIn("packages: write", store)
        self.assertNotIn("id-token:", store)
        self.assertIn("component-catalog.py publish", store)
        self.assertIn("check-production", job(workflow, "verified"))

    def test_python_signer_rejects_qualification_and_mismatched_targets(self):
        for change in ("qualification", "target", "build-test-context", "component", "role"):
            entry = copy.deepcopy(self.entries["build"])
            contract = entry["receipt"]["contract"]
            if change == "qualification":
                contract["role"] = "qualification"
                contract["inputs"]["scope"] = "qualification"
            elif change == "target":
                contract["inputs"]["targets"] = ["x86_64-unknown-linux-gnu"]
            elif change == "build-test-context":
                contract["inputs"]["component"] = "python/cp39-build-test-context"
            elif change == "component":
                contract["inputs"]["component"] = "toolchain/x86_64-install"
            else:
                contract["role"] = "python-row"
            entry["receipt_sha256"] = content_sha256(entry["receipt"])
            with self.subTest(change=change), self.assertRaises(IdentityError):
                component_catalog.document(self.producer, [entry], {"workflow": component_catalog.PYTHON_WORKFLOW, "event": "push"})

    def test_signing_cli_cannot_confuse_python_with_toolchain_handoff(self):
        value = self.handoff(["build"])
        path = self.root / "handoff.json"
        component_build.write_json(path, value)
        for mode in ("--python-ci", "--main-ci", None):
            output = self.root / ((mode or "pilot") + ".json")
            arguments = ["from-handoff", "--handoff", str(path), "--handoff-sha256", content_sha256(value), "--output", str(output)]
            if mode:
                arguments.append(mode)
            with mock.patch.object(component_ci, "checked_source", return_value=self.producer), \
                 mock.patch.dict(os.environ, GITHUB_EVENT_NAME="push"), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = CATALOG_CLI["main"](arguments)
            self.assertEqual(status == 0, mode == "--python-ci")
            self.assertEqual(output.exists(), mode == "--python-ci")
            if output.exists():
                self.assertEqual(load_json(output)["schema_version"], 3)

    def setup_resolver(self, name="x86_64-install"):
        self.name = name
        settings = self.specs[name]
        self.selection = {"status": "authenticated-reference", "entry": self.entries[name],
            "catalog": {"reference": component_handoff.REPOSITORY + "@sha256:" + "d" * 64},
            "authentication": {"producer": self.producer}}
        patches = [(component_build, "execution_identity", {"return_value": self.execution}),
                   (python_components, "spec", {"return_value": settings}),
                   (python_components, "bind_build", {"return_value": ({"fixture": "bound"}, {"fixture": "binding"})}),
                   (python_components, "inputs", {"return_value": self.expected[name]}),
                   (catalog_registry, "lookup", {"return_value": self.selection}),
                   (registry_transfer, "fetch", {}),
                   (component_build, "verify_local", {"return_value": "verified fixture context"})]
        self.patches = {name: mock.patch.object(module, name, **kwargs).start() for module, name, kwargs in patches}
        self.addCleanup(mock.patch.stopall)

    def resolve(self, reference=None, subjects=None):
        settings = self.specs[self.name]
        return component_resolution.python(self.root, {"fixture": "original"}, "cp39", settings["arch"], settings["kind"],
            self.execution, subjects or {}, self.root / "cosign", self.root / "resolution", "builder", self.root / "oras",
            self.root / "docker", reference)

    def test_resolution_verifies_dependencies_before_catalog_and_bytes_before_context(self):
        self.setup_resolver()
        events = []
        self.patches["bind_build"].side_effect = lambda *args: events.append("dependencies") or ({"fixture": "bound"}, {"fixture": "binding"})
        self.patches["lookup"].side_effect = lambda *args, **kwargs: events.append("catalog") or self.selection
        self.patches["fetch"].side_effect = lambda *args, **kwargs: events.append("fetch")
        self.patches["verify_local"].side_effect = lambda *args: events.append("actual bytes") or "verified fixture context"
        result = self.resolve(subjects={"original": "dependency subjects"})
        self.assertEqual(events, ["dependencies", "catalog", "fetch", "actual bytes"])
        self.assertEqual(self.patches["bind_build"].call_args[0][4], {"original": "dependency subjects"})
        self.assertEqual(self.patches["inputs"].call_args[0][1], {"fixture": "bound"})
        self.assertEqual(self.patches["inputs"].call_args[0][4], {"fixture": "binding"})
        self.assertEqual(result["producer"], self.producer)
        self.assertEqual(load_json(result["subject"]["receipt"]), self.entries[self.name]["receipt"])
        self.assertEqual(result["status"], "verified-build-component")
        self.assertNotIn("qualification", result)

    def test_missing_python_index_requests_build_without_accepted_subject(self):
        self.setup_resolver("build")
        self.patches["lookup"].return_value = {"status": "missing", "reason": "catalog-index-absent", "input_tag": "fixture"}
        result = self.resolve()
        self.assertEqual(result["status"], "build-required")
        self.assertNotIn("subject", result)
        self.patches["fetch"].assert_not_called()
        self.patches["verify_local"].assert_not_called()

    def test_bad_dependencies_fail_before_registry_access(self):
        self.setup_resolver()
        self.patches["bind_build"].side_effect = IdentityError("dependency digest differs")
        with self.assertRaisesRegex(IdentityError, "dependency digest differs"):
            self.resolve()
        self.patches["lookup"].assert_not_called()

    def test_signature_or_transfer_error_cannot_become_a_new_build_request(self):
        self.setup_resolver()
        self.patches["lookup"].side_effect = subprocess.CalledProcessError(1, "cosign")
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve()
        self.patches["fetch"].assert_not_called()
        self.assertFalse((self.root / "resolution/resolution.json").exists())

    def test_python_input_or_environment_drift_rejects_result(self):
        self.setup_resolver()
        changed = copy.deepcopy(self.expected[self.name])
        changed["parameters"]["changed"] = True
        self.patches["inputs"].side_effect = [self.expected[self.name], changed]
        with self.assertRaises(IdentityError):
            self.resolve()
        self.assertFalse((self.root / "resolution/resolution.json").exists())

    def test_fixed_python_recovery_reference_cannot_fall_back_to_production(self):
        self.setup_resolver()
        self.patches["lookup"].return_value = {"status": "missing"}
        reference = component_handoff.REPOSITORY + "@sha256:" + "d" * 64
        with self.assertRaisesRegex(IdentityError, "recovery catalog"):
            self.resolve(reference)
        self.assertEqual(self.patches["lookup"].call_args[1]["catalog_reference"], reference)

    def run_producer(self, requested=None, missing=(), fail_toolchain=False):
        order = []
        def toolchain(source, graph, arch, *args):
            order.append("toolchain/" + arch)
            return {"status": "build-required" if fail_toolchain else "verified-build-component",
                    "subject": {"receipt": "toolchain-" + arch, "receipt_sha256": "a" * 64, "layout": "fixture"}}
        def resolve(source, graph, row, arch, kind, execution, dependencies, *args):
            name = "build" if arch == "build" else arch + "-" + kind
            order.append("resolve/" + name)
            if arch != "build":
                self.assertIn("build-python", dependencies)
                self.assertIn("toolchain-" + arch, dependencies["toolchain-install"]["receipt"])
            result = {"status": "build-required" if name in missing else "verified-build-component",
                      "inputs_sha256": component_inputs.identity(self.expected[name])}
            if name not in missing:
                result["subject"] = {"receipt": "prior-" + name, "receipt_sha256": self.entries[name]["receipt_sha256"], "layout": "prior-fixture"}
            return result
        def produce(source, graph, row, arch, kind, execution, producer, dependencies, output, *args):
            name = "build" if arch == "build" else arch + "-" + kind
            order.append("produce/" + name)
            receipt = self.entries[name]["receipt"]
            component_build.write_json(output / "receipt.json", receipt)
            return {"receipt_sha256": content_sha256(receipt), "artifact": receipt["artifact"]}
        def publish(layout, digest, repository, *args):
            return {"reference": repository + "@" + digest}
        with mock.patch.object(component_ci, "checked_source", return_value=self.producer), \
             mock.patch.object(component_ci, "source_graph", return_value={}), \
             mock.patch.object(component_build, "execution_identity", return_value=self.execution), \
             mock.patch.object(component_resolution, "toolchain", side_effect=toolchain), \
             mock.patch.object(component_resolution, "python", side_effect=resolve), \
             mock.patch.object(python_components, "produce", side_effect=produce) as build, \
             mock.patch.object(registry_transfer, "publish", side_effect=publish) as push:
            try:
                result = ci_python.ensure(ROOT, "cp39", requested or sorted(self.entries), self.root / "producer", "builder", Path("oras"), Path("cosign"))
            finally:
                self.order, self.build_calls, self.publish_calls = order, build.call_args_list, push.call_args_list
        return result

    def test_producer_builds_only_misses_in_dependency_order_and_preserves_prior_subjects(self):
        result = self.run_producer(missing=["build", "aarch64-test-context"])
        self.assertEqual(self.order[:4], ["toolchain/x86_64", "toolchain/aarch64", "resolve/build", "produce/build"])
        self.assertEqual(result["new_parts"], ["aarch64-test-context", "build"])
        self.assertEqual(len(self.build_calls), 2)
        self.assertEqual(len(self.publish_calls), 2)
        self.assertEqual(result["subjects"]["x86_64-install"]["receipt"], "prior-x86_64-install")
        handoff = python_handoff.verify(ROOT, load_json(result["handoff"]), result["handoff_sha256"],
            self.producer["source_commit"], self.producer["invocation"])
        self.assertEqual(sorted(handoff["components"]), result["new_parts"])

    def test_available_parts_never_publish_or_create_a_new_handoff(self):
        result = self.run_producer()
        self.assertFalse(result["produced"])
        self.assertEqual(self.build_calls, [])
        self.assertEqual(self.publish_calls, [])
        self.assertNotIn("handoff", result)
        self.assertEqual(len(result["subjects"]), 7)

    def test_single_target_work_does_not_request_the_other_toolchain(self):
        self.run_producer(requested=["aarch64-install", "build"])
        self.assertEqual(self.order, ["toolchain/aarch64", "resolve/build", "resolve/aarch64-install"])

    def test_missing_toolchain_stops_python_production(self):
        with self.assertRaisesRegex(IdentityError, "prepared toolchain"):
            self.run_producer(fail_toolchain=True)
        self.assertEqual(self.build_calls, [])
        self.assertEqual(self.publish_calls, [])


if __name__ == "__main__":
    unittest.main()

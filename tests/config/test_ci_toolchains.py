"""Producer scheduling and publication never reinterpret errors as misses."""

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import ci_toolchains as ci, component_artifacts, component_build, component_ci
    from crossforge_internal import component_handoff, component_inputs
    from crossforge_internal.identity import IdentityError, content_sha256, load_json
    STAGES = runpy.run_path(str(ROOT / "scripts/ci-build.py"))["STAGES"]
    CATALOG_CLI = runpy.run_path(str(ROOT / "scripts/component-catalog.py"))
finally:
    sys.path.pop(0)


class ToolchainPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "recipe").write_text("fixture")
        (self.root / "component").mkdir()
        self.producer = {"kind": "github-actions", "source_commit": "a" * 40, "source_dirty": False,
            "invocation": "https://github.com/eglinuxer/crossforge/actions/runs/123/attempts/2", "started_at": "2026-09-10T00:00:00Z"}
        self.execution = {"buildkit_image": "image@sha256:" + "b" * 64}
        self.expected, self.receipts = {}, {}
        for arch in ci.ARCHITECTURES:
            for role in component_handoff.ROLES:
                spec = component_build.toolchain_spec(arch, role)
                inputs = component_inputs.capture(self.root, spec["component"], "build", ["recipe"], [spec["triple"]],
                                                  parameters={"execution": self.execution})
                contract = component_artifacts.contract(role, inputs, self.producer)
                (self.root / component_artifacts.CONTRACT_PATH).write_text(json.dumps(contract))
                digest = "sha256:" + content_sha256(contract)
                observation = {"kind": "crossforge-oci-layout-observation", "schema_version": 1, "platform": "linux/amd64",
                    "root_digest": digest, "platform_digest": digest, "config_digest": "sha256:" + "c" * 64}
                receipt = component_artifacts.receipt(contract, observation, self.root, [component_artifacts.CONTRACT_PATH])
                self.expected[arch, role] = inputs
                self.receipts[arch, role] = receipt
        self.environment = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "eglinuxer/crossforge",
            "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "a" * 40,
            "GITHUB_WORKFLOW_SHA": "a" * 40, "GITHUB_WORKFLOW_REF": component_ci.MAIN_CALLER,
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}

    def test_writer_requires_main_ci_caller_clean_checkout_and_matching_workflow_revision(self):
        for event in ("push", "workflow_dispatch"):
            producer = component_ci.github_producer(dict(self.environment, GITHUB_EVENT_NAME=event), "a" * 40, False, "main")
            self.assertEqual(producer["invocation"], self.producer["invocation"])
        for key, value in (("GITHUB_WORKFLOW_REF", component_ci.MAIN_CALLER.replace("ci.yml", "candidate.yml")),
                           ("GITHUB_WORKFLOW_REF", ""), ("GITHUB_WORKFLOW_SHA", "b" * 40),
                           ("GITHUB_EVENT_NAME", "pull_request_target"), ("GITHUB_EVENT_NAME", "schedule"),
                           ("GITHUB_REF", "refs/pull/1/merge"), ("GITHUB_REPOSITORY", "fork/crossforge"),
                           ("GITHUB_RUN_ID", "abc"), ("GITHUB_RUN_ATTEMPT", "0"), ("GITHUB_SHA", "b" * 40)):
            with self.subTest(key=key), self.assertRaises(IdentityError):
                component_ci.github_producer(dict(self.environment, **{key: value}), "a" * 40, False, "main")
        with self.assertRaises(IdentityError):
            component_ci.github_producer(self.environment, "a" * 40, True, "main")

    def run_ensure(self, arch, requested, missing=(), error=None):
        def inputs(source, graph, found_arch, role, execution):
            return self.expected[found_arch, role]
        def lookup(source, expected, role, *args, **kwargs):
            if error:
                raise error
            if role in missing:
                return {"status": "missing", "reason": "catalog-index-absent", "input_tag": "fixture"}
            receipt = copy.deepcopy(self.receipts[arch, role])
            # This original producer must never enter the new same-run catalog.
            receipt["contract"]["producer"]["invocation"] = receipt["contract"]["producer"]["invocation"].replace("123/", "100/")
            return {"status": "authenticated-reference", "entry": {"receipt": receipt,
                "receipt_sha256": content_sha256(receipt), "reference": component_handoff.REPOSITORY + "@" + receipt["artifact"]["root_digest"]}}
        def produce(source, graph, found_arch, role, execution, producer, output, *args):
            receipt = self.receipts[found_arch, role]
            component_build.write_json(output / "receipt.json", receipt)
            return {"receipt_sha256": content_sha256(receipt), "artifact": receipt["artifact"]}
        def publish(layout, digest, repository, *args):
            return {"reference": repository + "@" + digest}
        with mock.patch.object(component_ci, "checked_source", return_value=self.producer), \
             mock.patch.object(component_ci, "source_graph", return_value={"target": {}}), \
             mock.patch.object(component_build, "execution_identity", return_value=self.execution), \
             mock.patch.object(component_build, "toolchain_inputs", side_effect=inputs), \
             mock.patch.object(component_build, "produce_toolchain", side_effect=produce) as build, \
             mock.patch.object(ci.catalog_registry, "lookup", side_effect=lookup) as find, \
             mock.patch.object(ci.registry_transfer, "publish", side_effect=publish) as push:
            try:
                result = ci.ensure(ROOT, arch, requested, self.root / "output", "builder", Path("oras"), Path("cosign"))
            finally:
                self.build_calls, self.publish_calls, self.lookup_calls = build.call_args_list, push.call_args_list, find.call_args_list
        return result

    def test_all_available_preserves_original_producers_without_build_publish_or_handoff(self):
        with mock.patch.dict(os.environ, DOCKER_CONFIG="/task/docker"):
            result = self.run_ensure("x86_64", sorted(component_handoff.ROLES))
        self.assertFalse(result["produced"])
        self.assertEqual(self.build_calls, [])
        self.assertEqual(self.publish_calls, [])
        self.assertFalse((self.root / "output/handoff.json").exists())
        self.assertEqual(set(result["available"]), set(component_handoff.ROLES))
        for value in result["available"].values():
            self.assertIn("/runs/100/", value["entry"]["receipt"]["contract"]["producer"]["invocation"])
        self.assertEqual(self.lookup_calls[0][0][-1], Path("/task/docker/config.json"))

    def test_mixed_availability_publishes_only_missing_role_and_excludes_prior_producer(self):
        result = self.run_ensure("x86_64", sorted(component_handoff.ROLES), missing=["gcc-test-context"])
        self.assertTrue(result["produced"])
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(self.build_calls[0][0][3], "gcc-test-context")
        self.assertEqual(len(self.publish_calls), 1)
        handoff = component_handoff.verify(load_json(result["handoff"]), result["handoff_sha256"],
            self.producer["source_commit"], self.producer["invocation"])
        self.assertEqual(list(handoff["components"]), ["gcc-test-context"])
        self.assertEqual(handoff["architecture"], "x86_64")
        self.assertEqual(handoff["producer"], self.producer)

    def test_aarch64_component_handoff_remains_independent(self):
        result = self.run_ensure("aarch64", ["toolchain-install"], missing=["toolchain-install"])
        handoff = component_handoff.verify(load_json(result["handoff"]), result["handoff_sha256"],
            self.producer["source_commit"], self.producer["invocation"])
        receipt = handoff["components"]["toolchain-install"]["receipt"]
        self.assertEqual(receipt["contract"]["inputs"]["targets"], ["aarch64-unknown-linux-gnu"])
        self.assertEqual(len(self.build_calls), 1)

    def test_authentication_failure_cannot_schedule_or_publish_a_replacement(self):
        with self.assertRaisesRegex(IdentityError, "signature rejected"):
            self.run_ensure("x86_64", ["toolchain-install"], error=IdentityError("signature rejected"))
        self.assertEqual(self.build_calls, [])
        self.assertEqual(self.publish_calls, [])
        self.assertFalse((self.root / "output/handoff.json").exists())

    def test_catalog_cli_keeps_main_and_legacy_signing_entry_points_separate(self):
        components = {role: {"receipt": self.receipts["x86_64", role],
            "receipt_sha256": content_sha256(self.receipts["x86_64", role]),
            "reference": component_handoff.REPOSITORY + "@" + self.receipts["x86_64", role]["artifact"]["root_digest"]}
            for role in component_handoff.ROLES}
        for index, (version, main, accepted) in enumerate(((1, False, True), (2, True, True), (1, True, False), (2, False, False))):
            value = component_handoff.document(self.producer, self.execution, components, "x86_64" if version == 2 else None)
            handoff = self.root / ("handoff-%d.json" % index)
            output = self.root / ("catalog-%d.json" % index)
            component_build.write_json(handoff, value)
            arguments = ["from-handoff", "--handoff", str(handoff), "--handoff-sha256", content_sha256(value), "--output", str(output)]
            if main:
                arguments.append("--main-ci")
            with self.subTest(version=version, main=main), mock.patch.dict(os.environ, **self.environment), \
                 mock.patch.object(component_ci, "checked_source", return_value=self.producer) as source, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = CATALOG_CLI["main"](arguments)
            self.assertEqual(status == 0, accepted)
            self.assertEqual(source.call_args[0][1], "main" if main else "pilot")
            self.assertEqual(output.exists(), accepted)
            if accepted:
                catalog = load_json(output)
                self.assertEqual(catalog["schema_version"], version)
                self.assertEqual(catalog["producer"], self.producer)
                self.assertEqual("signing" in catalog, main)

    def test_invalid_requests_fail_before_checkout_or_credentials(self):
        for arch, requested in (("arm", ["toolchain-install"]), ("x86_64", []), ("x86_64", ["qualification"]),
                                ("x86_64", ["toolchain-install", "toolchain-install"]), ("x86_64", [True]),
                                ("x86_64", list(component_handoff.ROLES))):
            with mock.patch.object(component_ci, "checked_source") as source, self.subTest(arch=arch, roles=requested):
                with self.assertRaises(IdentityError):
                    ci.ensure(ROOT, arch, requested, self.root / "output", "builder", Path("oras"), Path("cosign"))
                source.assert_not_called()

    def test_plan_uses_selected_graph_edges_and_never_builds_for_no_work(self):
        with mock.patch.object(component_ci, "source_graph") as graph:
            result = ci.plan(ROOT, "", "none", STAGES, self.root / "graph", "builder")
        graph.assert_not_called()
        self.assertEqual(result["roles"], {arch: [] for arch in ci.ARCHITECTURES})
        selection = {"schema_version": 1, "kind": "crossforge-ci-source-plan", "mode": "incremental",
                     "targets": {"python-cp39": ["python-cp39-dev"]}}
        graph = {"target": {"python-cp39-dev": {"contexts": {"compiler": "target:toolchain-aarch64-build-export"}},
                           "toolchain-aarch64-build-export": {"target": "toolchain-aarch64-build-export"}}}
        with mock.patch.object(component_ci, "source_graph", return_value=graph) as read:
            result = ci.plan(ROOT, json.dumps(selection), "python", STAGES, self.root / "graph", "builder")
        self.assertEqual(read.call_args[0][1], ["python-cp39-dev"])
        self.assertEqual(result["roles"], {"aarch64": ["toolchain-install"], "x86_64": []})

    def test_all_expected_production_statuses_and_failures_are_checked(self):
        for produced in (True, False):
            results = {"ensure": {"result": "success", "outputs": {"produced": "true" if produced else "false"}},
                       "sign": {"result": "success" if produced else "skipped"},
                       "store": {"result": "success" if produced else "skipped"}}
            self.assertTrue(ci.check_production(results))
            self.mutate_statuses(ci.check_production, results)
            results["ensure"]["outputs"]["produced"] = ""
            self.assertFalse(ci.check_production(results))

    def test_selected_architecture_producers_cannot_be_skipped(self):
        for selected in ([], ["x86_64"], ["aarch64"], list(ci.ARCHITECTURES)):
            results = {arch: {"result": "success" if arch in selected else "skipped"} for arch in ci.ARCHITECTURES}
            results["plan"] = {"result": "success", "outputs": {arch + "-roles": '["toolchain-install"]' if arch in selected else '[]'
                                                                  for arch in ci.ARCHITECTURES}}
            results["plan"]["outputs"]["selection"] = json.dumps({"schema_version": 1, "kind": "crossforge-ci-source-plan",
                "mode": "incremental", "targets": {"toolchain-" + arch: ["toolchain-" + arch + "-dev"] for arch in selected}})
            results["plan"]["outputs"]["python-parts"] = "{}"
            self.assertTrue(ci.check_ready(results, STAGES))
            self.mutate_statuses(lambda value: ci.check_ready(value, STAGES), results)
            for value in ('["qualification"]', '{}', 'null', 'false', '["toolchain-install","toolchain-install"]'):
                changed = copy.deepcopy(results)
                changed["plan"]["outputs"]["x86_64-roles"] = value
                self.assertFalse(ci.check_ready(changed, STAGES))
            for key, value in (("selection", ""), ("selection", "null"), ("selection", "{}"), ("unknown", "true")):
                changed = copy.deepcopy(results)
                changed["plan"]["outputs"][key] = value
                self.assertFalse(ci.check_ready(changed, STAGES))
            del results["plan"]["outputs"]["selection"]
            self.assertFalse(ci.check_ready(results, STAGES))

    def mutate_statuses(self, check, results):
        for job in results:
            for status in ("success", "skipped", "failure", "cancelled", None):
                if status != results[job]["result"]:
                    changed = copy.deepcopy(results)
                    changed[job]["result"] = status
                    self.assertFalse(check(changed), (job, status))
            changed = copy.deepcopy(results)
            del changed[job]
            self.assertFalse(check(changed))
        self.assertFalse(check(dict(results, unexpected={"result": "success"})))


if __name__ == "__main__":
    unittest.main()

"""Raw-component retries retain producers and exact signed catalog bytes."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import unittest
from unittest import mock

import test_ci_toolchains as toolchain_fixtures
import test_python_ci_components as python_fixtures
import test_ci_component_routing as workflow_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_catalog, component_ci, component_handoff, component_retry as retry
    from crossforge_internal.identity import IdentityError, canonical_bytes, content_sha256, load_json
    CATALOG = runpy.run_path(str(ROOT / "scripts/component-catalog.py"))
    CLI = runpy.run_path(str(ROOT / "scripts/component-retry.py"))
finally:
    sys.path.pop(0)


class ComponentRetryTests(unittest.TestCase):
    def setUp(self):
        self.toolchain = toolchain_fixtures.ToolchainPreparationTests()
        self.toolchain.setUp()
        self.addCleanup(self.toolchain.doCleanups)
        self.python = python_fixtures.PythonCIComponentsTests()
        self.python.setUp()
        self.addCleanup(self.python.doCleanups)
        self.root = self.toolchain.root
        self.producer = self.toolchain.producer
        self.original = self.producer["invocation"]
        self.signer = dict(self.producer, invocation=self.original.replace("attempts/2", "attempts/3"))
        self.current = dict(self.producer, invocation=self.original.replace("attempts/2", "attempts/4"))
        self.ensure = {"ensure": {"result": "success", "outputs": {"produced": "true", "artifact-id": "101",
            "handoff-sha256": "a" * 64, "producer-invocation": self.original}}}

    def handoff(self, architecture="x86_64"):
        entries = {role: {"receipt": self.toolchain.receipts[architecture, role],
            "receipt_sha256": content_sha256(self.toolchain.receipts[architecture, role]),
            "reference": component_handoff.REPOSITORY + "@" + self.toolchain.receipts[architecture, role]["artifact"]["root_digest"]}
            for role in component_handoff.ROLES}
        return component_handoff.document(self.producer, self.toolchain.execution, entries, architecture)

    def catalog_cli(self, value, mode, directory, current=None, invocation=None, digest=None):
        directory.mkdir()
        path, output = directory / "handoff.json", directory / "catalog.json"
        path.write_text(json.dumps(value))
        arguments = ["from-handoff", "--handoff", str(path), "--handoff-sha256", digest or content_sha256(value), "--output", str(output)]
        if mode:
            arguments.append(mode)
        if invocation is not None:
            arguments += ["--producer-invocation", invocation]
        with mock.patch.object(component_ci, "checked_source", return_value=current or self.signer), \
             mock.patch.dict(os.environ, GITHUB_EVENT_NAME="workflow_dispatch"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = CATALOG["main"](arguments)
        return status, output

    def signed_fixture(self):
        catalog = component_catalog.document(self.producer, list(self.handoff()["components"].values()),
            {"workflow": component_catalog.MAIN_WORKFLOW, "event": "workflow_dispatch"})
        directory = self.root / "signed"
        directory.mkdir()
        (directory / "catalog.json").write_bytes(canonical_bytes(catalog) + b"\n")
        # Metadata-only fixture: live Cosign verification remains mandatory in
        # the workflow and in catalog_registry.publish, outside this helper.
        (directory / "catalog.sigstore.json").write_text('{"fixture":"unsigned"}\n')
        (directory / "authentication.json").write_text('{"fixture":true}\n')
        output = retry.catalog_metadata(directory, self.signer, self.original)
        output.update({"artifact-id": "102", "signer-invocation": self.signer["invocation"]})
        return directory, {"sign": {"result": "success", "outputs": output}}

    def test_retries_require_one_exact_run_and_ordered_original_attempts(self):
        self.assertEqual(retry.prior_invocation(self.original, self.current["invocation"]), self.original)
        for value in (self.original.replace("123/", "124/"), self.original.replace("attempts/2", "attempts/5"),
                      self.original.replace("eglinuxer/", "fork/"), self.original + "/", self.original + "\n",
                      self.original.replace("attempts/2", "attempts/02"), "", None):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                retry.prior_invocation(value, self.current["invocation"])

    def test_missing_failed_or_malformed_upstream_outputs_never_select_an_artifact(self):
        self.assertEqual(retry.upstream(self.ensure, "sign", self.signer), self.ensure["ensure"]["outputs"])
        for mode in ("failure", "skipped", "cancelled", "missing", "extra", "artifact", "empty", "hash", "produced", "no-output"):
            value = copy.deepcopy(self.ensure)
            if mode in ("failure", "skipped", "cancelled"):
                value["ensure"]["result"] = mode
            elif mode == "missing":
                del value["ensure"]["outputs"]["producer-invocation"]
            elif mode == "extra":
                value["ensure"]["outputs"]["unexpected"] = "yes"
            elif mode in ("artifact", "empty"):
                value["ensure"]["outputs"]["artifact-id"] = "101,102" if mode == "artifact" else ""
            elif mode == "hash":
                value["ensure"]["outputs"]["handoff-sha256"] = "tag"
            elif mode == "produced":
                value["ensure"]["outputs"]["produced"] = "false"
            else:
                value["ensure"]["outputs"] = None
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                retry.upstream(value, "sign", self.signer)

    def test_toolchain_and_python_signing_retries_preserve_catalog_bytes_and_original_producer(self):
        for name, value, mode in (("x86", self.handoff(), "--main-ci"),
                                  ("arm", self.handoff("aarch64"), "--main-ci"),
                                  ("python", self.python.handoff(["build", "aarch64-install"]), "--python-ci")):
            originals = []
            for attempt in (3, 4):
                current = dict(self.producer, invocation=self.original.replace("attempts/2", "attempts/" + str(attempt)))
                status, output = self.catalog_cli(value, mode, self.root / (name + str(attempt)), current, self.original)
                self.assertEqual(status, 0)
                originals.append(output.read_bytes())
                catalog = load_json(output)
                self.assertEqual(catalog["producer"], value["producer"])
                self.assertEqual({entry["reference"] for entry in catalog["entries"]},
                                 {entry["reference"] for entry in value["components"].values()})
            self.assertEqual(originals[0], originals[1])

    def test_signing_requires_the_original_independent_invocation_and_handoff_digest(self):
        value = self.handoff()
        for index, (invocation, digest, current) in enumerate(((None, None, self.signer),
                (self.signer["invocation"], None, self.signer), (self.original, "b" * 64, self.signer),
                (self.original.replace("123/", "124/"), None, self.signer),
                (self.original, None, dict(self.signer, source_commit="b" * 40)))):
            status, output = self.catalog_cli(value, "--main-ci", self.root / ("rejected-" + str(index)), current, invocation, digest)
            self.assertEqual(status, 1)
            self.assertFalse(output.exists())
        legacy = component_handoff.document(self.producer, self.toolchain.execution, value["components"])
        status, output = self.catalog_cli(legacy, None, self.root / "pilot", self.producer, self.original)
        self.assertEqual(status, 1)
        self.assertFalse(output.exists())

    def test_storage_retry_uses_exact_original_signed_pair_without_relabeling(self):
        directory, needs = self.signed_fixture()
        original = {path.name: path.read_bytes() for path in directory.iterdir()}
        with mock.patch.object(component_ci, "checked_source", return_value=self.current), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CLI["main"](["upstream", "--stage", "store", "--needs-json", json.dumps(needs)]), 0)
            self.assertEqual(CLI["main"](["verify-catalog", "--directory", str(directory), "--needs-json", json.dumps(needs)]), 0)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, original)
        for key, value in (("signer-invocation", self.original.replace("attempts/2", "attempts/1")),
                           ("signer-invocation", self.original.replace("attempts/2", "attempts/5")),
                           ("producer-invocation", self.original.replace("123/", "124/"))):
            changed = copy.deepcopy(needs)
            changed["sign"]["outputs"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(IdentityError):
                retry.verify_catalog(directory, changed, self.current)

    def test_storage_rejects_changed_bytes_noncanonical_or_incomplete_artifacts(self):
        original, needs = self.signed_fixture()
        for mode in ("catalog", "bundle", "source", "encoding", "extra", "missing", "symlink"):
            directory = self.root / mode
            shutil.copytree(str(original), str(directory))
            if mode in ("catalog", "source"):
                value = load_json(directory / "catalog.json")
                if mode == "catalog":
                    value["signing"]["event"] = "push"
                else:
                    value["producer"]["source_commit"] = "b" * 40
                (directory / "catalog.json").write_bytes(canonical_bytes(value) + b"\n")
            elif mode == "bundle":
                (directory / "catalog.sigstore.json").write_text("different signed bytes")
            elif mode == "encoding":
                (directory / "catalog.json").write_text(json.dumps(load_json(directory / "catalog.json"), indent=2))
            elif mode == "extra":
                (directory / "oci").mkdir()
            elif mode == "missing":
                (directory / "authentication.json").unlink()
            else:
                (directory / "catalog.sigstore.json").unlink()
                (directory / "catalog.sigstore.json").symlink_to(original / "catalog.sigstore.json")
            with self.subTest(mode=mode), self.assertRaises((ValueError, OSError)):
                retry.verify_catalog(directory, needs, self.current)

    def test_catalog_outputs_record_original_producer_and_current_signer_only_after_metadata_validation(self):
        directory, needs = self.signed_fixture()
        stream = io.StringIO()
        with mock.patch.object(component_ci, "checked_source", return_value=self.signer), contextlib.redirect_stdout(stream):
            status = CLI["main"](["catalog-outputs", "--directory", str(directory), "--producer-invocation", self.original])
        self.assertEqual(status, 0)
        output = dict(line.split("=", 1) for line in stream.getvalue().splitlines())
        self.assertEqual(output, {key: value for key, value in needs["sign"]["outputs"].items() if key != "artifact-id"})
        with mock.patch.object(component_ci, "checked_source", side_effect=IdentityError("untrusted caller")), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI["main"](["catalog-outputs", "--directory", str(directory), "--producer-invocation", self.original]), 1)
            self.assertEqual(stdout.getvalue(), "")


class ComponentRetryWorkflowTests(unittest.TestCase):
    def test_producer_workflows_validate_original_outputs_before_download_and_sign_or_store(self):
        for filename in ("produce-toolchain.yml", "produce-python.yml", "produce-python-row.yml"):
            workflow = (ROOT / ".github/workflows" / filename).read_text()
            sign, store = (workflow_fixtures.job(workflow, name) for name in ("sign", "store"))
            for stage, block, upstream in (("sign", sign, "ensure"), ("store", store, "sign")):
                self.assertIn("actions: read", block)
                self.assertLess(block.index("component-retry.py upstream --stage " + stage), block.index("actions/download-artifact@"))
                self.assertIn("artifact-ids: ${{ needs.%s.outputs.artifact-id }}" % upstream, block)
                self.assertIn("github-token: ${{ github.token }}", block)
                self.assertIn("run-id: ${{ github.run_id }}", block)
            self.assertNotIn("packages: write", sign)
            self.assertNotIn("id-token: write", store)
            self.assertIn('--producer-invocation "$PRODUCER_INVOCATION"', sign)
            self.assertLess(sign.index("component-catalog.py verify"), sign.index("component-retry.py catalog-outputs"))
            self.assertLess(store.index("component-retry.py verify-catalog"), store.index("docker login"))
            self.assertLess(store.index("component-retry.py verify-catalog"), store.index("component-catalog.py publish"))
            self.assertNotIn("sign-blob", store)
            self.assertNotIn("ci-toolchains.py ensure", sign + store)
            self.assertNotIn("ci-python.py ensure", sign + store)

    def test_artifact_read_permission_reaches_producers_through_every_reusable_caller(self):
        for filename, jobs in (("ci.yml", ("builds-components",)), ("candidate.yml", ("qualify",)),
                              ("verify-main-incremental.yml", ("x86_64", "aarch64", "builds")),
                              ("verify-main-builds.yml", ("python-components",))):
            workflow = (ROOT / ".github/workflows" / filename).read_text()
            for name in jobs:
                with self.subTest(workflow=filename, job=name):
                    self.assertIn("actions: read", workflow_fixtures.job(workflow, name))


if __name__ == "__main__":
    unittest.main()

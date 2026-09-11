"""Runtime overlays use scoped RPM inputs without weakening installed evidence."""

import copy
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_python_qualification as qualification_fixtures

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = runpy.run_path(str(ROOT / "scripts/assemble-python-runtime.py"))
POLICY = OVERLAY["OVERLAY"]
COMPONENTS = runpy.run_path(str(ROOT / "scripts/release-components-core.py"))
RELEASE = OVERLAY["load_json"](ROOT / "config/release.json")
DOCUMENTS = COMPONENTS["render_component_documents"](RELEASE)
BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
sys.path.insert(0, str(ROOT / "scripts"))
from crossforge_internal import bake_materials

EXECUTION = {"buildkit_image": "moby/buildkit:v0.33.0@sha256:6c2fa84a6b61ccd72899dde4239f8d5717f05f9a8ca6f3cad185fb1a95a94de3"}


class RuntimeComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def component(self, arch):
        name = "rpm/sysroot-" + arch
        return name, ROOT / "config/generated/components" / (name + ".json"), POLICY["canonical_sha256"](DOCUMENTS[name])

    def context(self, arch):
        name, path, digest = self.component(arch)
        return OVERLAY["MATERIALIZER"]["load_lock"](ROOT / ("locks/sysroot-el8-" + arch + ".json"),
            release_component=path, release_component_name=name, release_component_sha256=digest)

    def evidence(self, arch):
        context = self.context(arch)
        name, path, digest = self.component(arch)
        base = POLICY["component_base"](path, name, digest, arch)
        selected = OVERLAY["select_runtime_packages"](context, context["packages"])
        before = [("bash", arch, "bash-0:4.4.20-6.el8_10." + arch)]
        after = sorted(before + [(item["item"]["name"], item["item"]["arch"], item["item"]["nevra"]) for item in selected], key=lambda row: row[2])
        return OVERLAY["build_evidence"](context, None, base["manifest_digest"], selected, before, after,
                                          "1" * 64, component_base=base)

    def validate(self, value, release, arch):
        OVERLAY["validate_evidence"](value)
        POLICY["validate_identity_binding"](value, release, arch, COMPONENTS["render_component_documents"])

    def test_both_architectures_bind_the_actual_rpm_component_and_preserve_inventory(self):
        for arch in ("x86_64", "aarch64"):
            value = self.evidence(arch)
            self.validate(value, RELEASE, arch)
            self.assertEqual(value["schema_version"], 2)
            self.assertNotIn("release_sha256", value["identity"])
            self.assertEqual(value["identity"]["input_binding"], self.context(arch)["release_binding"])
            self.assertEqual(value["identity"]["base_image"]["manifest_digest"], RELEASE["base_image"]["manifests"]["amd64" if arch == "x86_64" else "arm64"])
            self.assertEqual(len(value["identity"]["selected_packages"]), 7)
            self.assertEqual(value["identity_sha256"], POLICY["canonical_sha256"](value["identity"]))

    def test_unrelated_product_and_python_changes_preserve_overlay_but_relevant_inputs_do_not(self):
        value = self.evidence("x86_64")
        for field in ("product", "python", "sysroot", "base", "trust"):
            release = copy.deepcopy(RELEASE)
            if field == "product":
                release["product"]["version"] = "0.1.1"
            elif field == "python":
                release["python"]["versions"][0]["source"]["sha256"] = "a" * 64
            elif field == "sysroot":
                release["targets"][0]["sysroot"]["canonical_sha256"] = "a" * 64
            elif field == "base":
                release["base_image"]["manifests"]["amd64"] = "sha256:" + "a" * 64
            else:
                release["trust"]["rocky_rpm_key"]["sha256"] = "a" * 64
            if field in ("product", "python"):
                self.validate(value, release, "x86_64")
            else:
                with self.assertRaises(POLICY["OverlayError"]):
                    self.validate(value, release, "x86_64")

    def test_schema_binding_and_independent_component_tampering_are_rejected(self):
        baseline = self.evidence("x86_64")
        for field, replacement in (("kind", "release-config"), ("component", "rpm/sysroot-aarch64"),
                                    ("scope", "qualification"), ("canonical_sha256", "a" * 64)):
            value = copy.deepcopy(baseline)
            value["identity"]["input_binding"][field] = replacement
            value["identity_sha256"] = POLICY["canonical_sha256"](value["identity"])
            with self.subTest(field=field), self.assertRaises((OVERLAY["ValidationError"], POLICY["OverlayError"])):
                self.validate(value, RELEASE, "x86_64")
        for version in (True, 1, 3, "2"):
            value = copy.deepcopy(baseline)
            value["schema_version"] = version
            with self.assertRaises((OVERLAY["ValidationError"], POLICY["OverlayError"])):
                self.validate(value, RELEASE, "x86_64")
        name, path, digest = self.component("x86_64")
        for selected_arch, selected_digest in (("aarch64", digest), ("x86_64", "b" * 64)):
            with self.assertRaises(POLICY["OverlayError"]):
                POLICY["component_base"](path, name, selected_digest, selected_arch)

    def cropped(self, arch):
        for name in ("assemble-python-runtime.py", "python_runtime_overlay.py", "release_component.py",
                     "materialize-sysroot.py", "validate-rpm-lock.py", "validate-release.py"):
            target = self.root / "scripts" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / "scripts" / name, target)
        for name in ("config/schemas/rpm-transaction.schema.json", "config/schemas/rpm-lock.schema.json",
                     "keys/RPM-GPG-KEY-rockyofficial",
                     "locks/sysroot-el8-" + arch + ".json", "locks/transactions/sysroot-el8-" + arch + ".json",
                     "config/generated/components/rpm/sysroot-" + arch + ".json"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        relative = "locks/metadata/sysroot-el8-" + arch
        shutil.copytree(str(ROOT / relative), str(self.root / relative))
        return runpy.run_path(str(self.root / "scripts/assemble-python-runtime.py"))

    def test_cropped_producer_needs_no_release_or_rpm_plan_and_verifies_the_whole_bundle(self):
        arch = "aarch64"
        isolated = self.cropped(arch)
        self.assertFalse((self.root / "config/release.json").exists())
        self.assertFalse((self.root / "config/schemas/release.schema.json").exists())
        self.assertFalse((self.root / "config/rpm").exists())
        name, _, digest = self.component(arch)
        path = self.root / "config/generated/components" / (name + ".json")
        context = isolated["MATERIALIZER"]["load_lock"](self.root / ("locks/sysroot-el8-" + arch + ".json"),
            release_component=path, release_component_name=name, release_component_sha256=digest)
        selected = isolated["select_runtime_packages"](context, context["packages"])
        root = self.root / "runtime"
        os_release = root / "usr/lib/os-release"
        os_release.parent.mkdir(parents=True)
        os_release.write_text('ID=rocky\nVERSION_ID="8.10"\n')
        original_sha = isolated["sha256_file"](os_release)
        bundle = self.root / "bundle"
        bundle.mkdir()
        before = [("bash", arch, "bash-0:4.4.20-6.el8_10." + arch)]
        after = sorted(before + [(item["item"]["name"], item["item"]["arch"], item["item"]["nevra"]) for item in selected], key=lambda row: row[2])
        calls = []
        def verify(actual, *args):
            self.assertEqual(actual["packages"], context["packages"])
            self.assertGreater(len(actual["packages"]), 7)
            calls.append("verify-all")
        def headers(actual, *args):
            verify(actual)
            return actual["packages"]
        def command(arguments, label):
            self.assertEqual(calls[:2], ["verify-all", "verify-all"])
            self.assertIn("--noscripts", arguments)
            self.assertIn("--notriggers", arguments)
            calls.append("transaction")
        function = isolated["assemble"]
        with mock.patch.dict(isolated["MATERIALIZER"], verify_bundle=verify, verify_key_and_headers=headers, command=command), \
             mock.patch.dict(function.__globals__, validate_runtime_root=lambda *args: original_sha,
                             rpm_inventory=mock.Mock(side_effect=[before, before, after])):
            function(self.root / ("locks/sysroot-el8-" + arch + ".json"), bundle, self.root / "key", root,
                RELEASE["base_image"]["manifests"]["arm64"], self.root / "evidence.json",
                release_component=path, release_component_name=name, release_component_sha256=digest)
        value = isolated["load_json"](self.root / "evidence.json")
        self.validate(value, RELEASE, arch)
        self.assertEqual(calls, ["verify-all", "verify-all", "transaction", "transaction"])

    def test_incomplete_or_mixed_component_mode_fails_before_runtime_mutation(self):
        name, path, digest = self.component("x86_64")
        function = OVERLAY["assemble"]
        valid = {"release_component": path, "release_component_name": name, "release_component_sha256": digest}
        for change in ({"release_component": None}, {"release_component_name": None}, {"release_component_sha256": None},
                       {"release_config": ROOT / "config/release.json"}, {"release_component_sha256": "0" * 64}):
            with mock.patch.dict(function.__globals__, require_runtime_root=mock.Mock()) as _patch:
                with self.assertRaises(OVERLAY["ValidationError"]):
                    function(ROOT / "locks/sysroot-el8-x86_64.json", self.root, self.root / "key", self.root,
                             RELEASE["base_image"]["manifests"]["amd64"], self.root / "never.json", **dict(valid, **change))
                function.__globals__["require_runtime_root"].assert_not_called()
            self.assertFalse((self.root / "never.json").exists())


class RuntimeComponentConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = qualification_fixtures.PythonQualificationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_final_qualification_accepts_new_overlay_while_preserving_exact_raw_evidence(self):
        fixture = self.fixture
        overlay = fixture.clean["runtime"]["overlay_evidence"]
        overlay["schema_version"] = 2
        overlay["identity"].pop("release_sha256")
        overlay["identity"]["input_binding"] = POLICY["binding_from_release"](fixture.release, "x86_64", COMPONENTS["render_component_documents"])
        overlay["identity_sha256"] = POLICY["canonical_sha256"](overlay["identity"])
        fixture.clean["runtime"]["identity_sha256"] = overlay["identity_sha256"]
        fixture.write_json(fixture.clean_path, fixture.clean)
        report = fixture.finalize()
        fixture.validate_final_report(report)
        self.assertEqual(report["executions"]["clean-rocky"]["runtime"]["overlay_evidence"], overlay)
        overlay["identity"]["input_binding"]["canonical_sha256"] = "0" * 64
        overlay["identity_sha256"] = POLICY["canonical_sha256"](overlay["identity"])
        fixture.clean["runtime"]["identity_sha256"] = overlay["identity_sha256"]
        fixture.write_json(fixture.clean_path, fixture.clean)
        with self.assertRaises(qualification_fixtures.FINALIZER["FinalizationError"]):
            fixture.finalize()

    def test_legacy_overlay_still_requires_its_original_full_release(self):
        fixture = self.fixture
        overlay = fixture.valid_overlay()
        POLICY["validate_identity_binding"](overlay, fixture.release, "x86_64", COMPONENTS["render_component_documents"])
        changed = copy.deepcopy(fixture.release)
        changed["product"]["version"] = "0.1.1"
        with self.assertRaises(POLICY["OverlayError"]):
            POLICY["validate_identity_binding"](overlay, changed, "x86_64", COMPONENTS["render_component_documents"])
        self.assertEqual(overlay["schema_version"], 1)

    def test_runtime_reader_checks_scoped_identity_and_actual_inventory(self):
        fixture = self.fixture
        value = fixture.valid_overlay()
        value["schema_version"] = 2
        value["identity"].pop("release_sha256")
        value["identity"]["input_binding"] = POLICY["binding_from_release"](fixture.release, "x86_64", COMPONENTS["render_component_documents"])
        value["identity_sha256"] = POLICY["canonical_sha256"](value["identity"])
        root = fixture.directory / "runtime-component-root"
        path = root / "usr/lib/os-release"
        path.parent.mkdir(parents=True)
        path.write_text('ID=rocky\nVERSION_ID="8.10"\n')
        inventory = sorted(value["runtime_inventory"]["installed_nevras"] + ["base-%d" % index for index in range(10)])
        value["runtime_inventory"]["after_item_count"] = len(inventory)
        value["runtime_inventory"]["after_sha256"] = POLICY["canonical_sha256"](inventory)
        value["runtime_inventory"]["os_release_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        runner = qualification_fixtures.RUNTIME_RUNNER
        function = runner["validate_overlay_evidence"]
        arguments = (fixture.release, runner["TARGETS"][qualification_fixtures.TARGET], qualification_fixtures.TARGET,
                     fixture.compile, root, OVERLAY["RUNTIME_PACKAGE_NAMES"])
        with mock.patch.dict(function.__globals__, run=mock.Mock(return_value=("\n".join(inventory) + "\n", ""))):
            self.assertEqual(function(value, *arguments), value)
            for field in ("binding", "image", "target", "lock", "inventory"):
                changed = copy.deepcopy(value)
                if field == "binding":
                    changed["identity"]["input_binding"]["canonical_sha256"] = "a" * 64
                elif field == "image":
                    changed["identity"]["base_image"]["manifest_digest"] = "sha256:" + "a" * 64
                elif field == "target":
                    changed["identity"]["target"]["arch"] = "aarch64"
                elif field == "lock":
                    changed["identity"]["sysroot"]["lock_sha256"] = "a" * 64
                else:
                    changed["runtime_inventory"]["after_sha256"] = "a" * 64
                changed["identity_sha256"] = POLICY["canonical_sha256"](changed["identity"])
                with self.subTest(field=field), self.assertRaises(runner["RuntimeError_"]):
                    function(changed, *arguments)

    def test_new_overlay_cannot_hide_a_changed_runtime_rpm(self):
        fixture = self.fixture
        value = fixture.clean["runtime"]["overlay_evidence"]
        value["schema_version"] = 2
        value["identity"].pop("release_sha256")
        value["identity"]["input_binding"] = POLICY["binding_from_release"](fixture.release, "x86_64", COMPONENTS["render_component_documents"])
        value["identity"]["selected_packages"][0]["received_sha256"] = "0" * 64
        value["identity"]["selected_packages_sha256"] = POLICY["canonical_sha256"](value["identity"]["selected_packages"])
        value["identity_sha256"] = POLICY["canonical_sha256"](value["identity"])
        fixture.clean["runtime"]["identity_sha256"] = value["identity_sha256"]
        fixture.write_json(fixture.clean_path, fixture.clean)
        with self.assertRaises(qualification_fixtures.FINALIZER["FinalizationError"]):
            fixture.finalize()


class RuntimeComponentGraphTests(unittest.TestCase):
    def test_actual_runtime_graphs_exclude_the_complete_release_and_preserve_pins(self):
        if not shutil.which("docker"):
            self.skipTest("runtime material graph checks require Buildx")
        for arch in ("x86_64", "aarch64"):
            target = "python-runtime-clean-" + arch
            graph = BUILD["read_graph"]([target])
            closure = bake_materials.source_closure(ROOT, graph, target, EXECUTION)
            paths = {item["path"] for item in closure["files"]}
            self.assertNotIn("config/release.json", paths)
            self.assertNotIn("config/schemas/release.schema.json", paths)
            self.assertNotIn("config/rpm/sysroot-el8-" + arch + ".plan.json", paths)
            self.assertIn("config/generated/components/rpm/sysroot-" + arch + ".json", paths)
            self.assertIn("locks/sysroot-el8-" + arch + ".json", paths)
            self.assertIn("scripts/python_runtime_overlay.py", paths)


if __name__ == "__main__":
    unittest.main()

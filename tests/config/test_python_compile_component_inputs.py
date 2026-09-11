import argparse
import copy
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_python_qualification as fixtures


ROOT = Path(__file__).resolve().parents[2]
QUALIFIER = fixtures.QUALIFIER
FINALIZER = fixtures.FINALIZER
RUNTIME = fixtures.RUNTIME_RUNNER
POLICY = QUALIFIER["POLICY"]
CORE = QUALIFIER["release_components"]()
SHA = CORE["canonical_sha256"]
ROW = runpy.run_path(str(ROOT / "docker/verify-python-row.py"))


class PythonCompileComponentInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = fixtures.RELEASE_CONFIG
        cls.components = ROOT / "config/generated/components"
        cls.documents = CORE["render_component_documents"](cls.release)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def arguments(self, version="3.9.25", arch="x86_64", directory=None):
        directory = self.components if directory is None else directory
        contract = CORE["ROW_CONTRACT"]["contract_for_version"](version)
        row = contract["row"]
        source = "python/%s-source" % row
        build_policy = "implementation/python-%s-build-policy" % row
        manifest = ROW["component_row_contract"](
            row, version, contract["adapter"], directory / (source + ".json"), SHA(self.documents[source]),
            directory / (build_policy + ".json"), SHA(self.documents[build_policy]))
        path = self.directory / (row + "-source.json")
        path.write_text(json.dumps(manifest))
        return argparse.Namespace(target=arch + "-unknown-linux-gnu", version=version, release=None,
            qualification_components=directory, source_manifest=path,
            qualification_component_sha256=SHA(self.documents["python/%s-%s-qualification" % (row, arch)]),
            qualification_policy_component_sha256=None)

    def test_all_twelve_producers_bind_actual_source_projections(self):
        for version in fixtures.IMPLEMENTED_VERSIONS:
            for arch in ("x86_64", "aarch64"):
                with self.subTest(version=version, arch=arch):
                    args = self.arguments(version, arch)
                    policy = POLICY["from_release"](self.release, version, arch, CORE["render_component_documents"])
                    inputs = QUALIFIER["qualification_inputs"](args)
                    self.assertIsNone(inputs["release"])
                    self.assertEqual(inputs["report_identity"], {
                        "qualification_schema_version": 5, "input_binding": POLICY["binding"](policy)})
                    self.assertEqual(inputs["source"], policy["source"])
                    self.assertEqual(inputs["abi"], policy["abi"])
                    self.assertEqual(inputs["sysroot_sha256"], policy["target"]["sysroot"]["canonical_sha256"])
                    self.assertEqual(inputs["zstd_components"], policy["zstd_components"])

    def test_cropped_static_stage_loads_without_full_release_or_renderer(self):
        directory = self.directory / "cropped"
        scripts = directory / "scripts"
        scripts.mkdir(parents=True)
        block = (ROOT / "docker/python.Dockerfile").read_text().split(" AS cpython-qualify-build\n", 1)[1].split("\n# Component exports", 1)[0]
        for path in (
            "docker/verify-python-row.py", "scripts/release_component.py", "scripts/qualify-cpython.py",
            "scripts/abi_contract.py", "scripts/python_abi_audit.py", "scripts/python_runtime_providers.py",
            "scripts/python_sdk_identity.py", "scripts/target_artifact_audit.py", "scripts/python_qualification_policy.py",
            "scripts/prepare-cpython-source.py", "scripts/python_row_contract.py", "scripts/python_zstd_evidence.py",
        ):
            self.assertIn(path, block)
            shutil.copyfile(ROOT / path, scripts / Path(path).name)
        self.assertNotIn("scripts/release-components-core.py", block)
        self.assertNotIn("config/release.json", block)
        self.assertNotIn("scripts/validate-release.py", block)
        component_directory = directory / "components"
        for arch in ("x86_64", "aarch64"):
            version = fixtures.ZSTD_VERSION
            policy = POLICY["from_release"](self.release, version, arch, CORE["render_component_documents"])
            names = [policy["component"]["component"], "implementation/python-cp314-qualification-policy"]
            names.extend(record["component"] for record in policy["source_components"].values())
            for name in names:
                target = component_directory / (name + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.components / (name + ".json"), target)
            args = self.arguments(version, arch, component_directory)
            code = ("import argparse,json,runpy,sys; from pathlib import Path; "
                    "q=runpy.run_path(sys.argv[1]); a=json.loads(sys.argv[2]); "
                    "a.update({k:Path(a[k]) for k in ('qualification_components','source_manifest')}); "
                    "value=q['qualification_inputs'](argparse.Namespace(**a)); "
                    "assert q['qualification_inputs'].__globals__['RELEASE_COMPONENTS'] is None; "
                    "print(json.dumps(value,sort_keys=True))")
            result = subprocess.run([sys.executable, "-c", code, str(scripts / "qualify-cpython.py"),
                json.dumps(vars(args), default=str)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), QUALIFIER["qualification_inputs"](args))

    def test_source_manifest_and_component_tampering_are_rejected(self):
        for mutation in (
            lambda value: value.update(version="3.9.24"),
            lambda value: value.update(schema_version=1),
            lambda value: value.update(extra=True),
        ):
            args = self.arguments()
            manifest = json.loads(args.source_manifest.read_text())
            mutation(manifest)
            args.source_manifest.write_text(json.dumps(manifest))
            with self.assertRaises(QUALIFIER["QualificationError"]):
                QUALIFIER["qualification_inputs"](args)
        copied = self.directory / "components"
        shutil.copytree(self.components, copied)
        args = self.arguments(directory=copied)
        path = copied / "python/cp39-source.json"
        value = json.loads(path.read_text())
        value["materials"][0]["value"] = "wrong"
        path.write_text(json.dumps(value))
        with self.assertRaises(QUALIFIER["QualificationError"]):
            QUALIFIER["qualification_inputs"](args)

    def test_input_modes_reject_mixed_missing_and_invalid_pins(self):
        mutations = (
            lambda a: setattr(a, "release", ROOT / "config/release.json"),
            lambda a: setattr(a, "source_manifest", None),
            lambda a: setattr(a, "qualification_components", None),
            lambda a: setattr(a, "qualification_policy_component_sha256", "0" * 64),
            lambda a: setattr(a, "qualification_component_sha256", "0" * 64),
            lambda a: setattr(a, "qualification_component_sha256", True),
        )
        for mutate in mutations:
            args = self.arguments()
            mutate(args)
            with self.subTest(mutate=mutate), self.assertRaises(QUALIFIER["QualificationError"]):
                QUALIFIER["qualification_inputs"](args)

    def test_legacy_cli_retains_original_identity(self):
        args = self.arguments()
        components = CORE["python_qualification_components"](self.release)
        args.release = ROOT / "config/release.json"
        args.qualification_components = args.source_manifest = None
        args.qualification_component_sha256 = components["aggregate"]["canonical_sha256"]
        args.qualification_policy_component_sha256 = components["policy"]["canonical_sha256"]
        inputs = QUALIFIER["qualification_inputs"](args)
        self.assertEqual(inputs["report_identity"], {"qualification_schema_version": 4,
            "release_sha256": SHA(self.release), "qualification_components": components})
        self.assertEqual(inputs["release"], self.release)

    def harness(self, version=fixtures.VERSION):
        harness = fixtures.PythonQualificationTests()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        harness.reset_fixture(version)
        identity = QUALIFIER["qualification_inputs"](self.arguments(version))["report_identity"]
        harness.compile.pop("release_sha256")
        harness.compile.pop("qualification_components")
        harness.compile.update(identity)
        harness.write_json(harness.compile_path, harness.compile)
        harness.refresh_runtime_bindings()
        return harness

    def validate_compile(self, harness, release=None):
        release = harness.release if release is None else release
        context = FINALIZER["release_context"](release, fixtures.TARGET, harness.version)
        context["release"] = release
        return FINALIZER["validate_compile_report"](harness.compile, context, fixtures.TARGET, harness.version,
            harness.abi_context, harness.actual_elf_evidence(), True)

    def test_all_six_scoped_compile_reports_enter_existing_runtime_and_final_gates(self):
        for version in fixtures.IMPLEMENTED_VERSIONS:
            with self.subTest(version=version):
                harness = self.harness(version)
                self.validate_compile(harness)
                RUNTIME["validate_compile_qualification_components"](harness.compile, harness.release)
                report = harness.finalize()
                self.assertEqual(report["qualification_schema_version"], 4)
                self.assertEqual(report["compile"]["qualification_schema_version"], 5)
                self.assertEqual(report["release_sha256"], SHA(harness.release))
                self.assertEqual(report["qualification_components"], CORE["python_qualification_components"](harness.release))
                self.assertEqual(report["compile"], harness.compile)
                harness.validate_final_report(report)

    def test_scoped_binding_does_not_weaken_artifact_and_abi_checks(self):
        for mutate in (
            lambda value: value["source"].update(sha256="0" * 64),
            lambda value: value.update(sysroot_sha256="0" * 64),
            lambda value: value["abi"]["baseline"].update(canonical_sha256="0" * 64),
            lambda value: value["target_artifact_guard"].update(denied_execution_attempts=99),
            lambda value: value["extension"].update(sha256="0" * 64),
        ):
            harness = self.harness()
            mutate(harness.compile)
            with self.subTest(mutate=mutate), self.assertRaises(FINALIZER["FinalizationError"]):
                self.validate_compile(harness)

    def test_scoped_compile_can_survive_product_change_but_final_report_cannot(self):
        harness = self.harness()
        changed = copy.deepcopy(harness.release)
        changed["product"]["version"] = "0.1.1"
        self.validate_compile(harness, changed)
        RUNTIME["validate_compile_qualification_components"](harness.compile, changed)
        report = harness.finalize()
        with self.assertRaises(FINALIZER["FinalizationError"]):
            FINALIZER["validate_final_report"](report, changed, fixtures.TARGET, harness.version,
                harness.abi_context, harness.actual_elf_evidence(), True)
        changed["python"]["versions"][4]["source"]["sigstore"]["bundle_sha256"] = "0" * 64
        with self.assertRaises(FINALIZER["FinalizationError"]):
            self.validate_compile(harness, changed)

    def test_schema_and_binding_mismatches_fail_in_both_readers(self):
        for mutate in (
            lambda value: value.update(qualification_schema_version=5.0),
            lambda value: value.update(qualification_schema_version=6),
            lambda value: value.update(release_sha256=SHA(self.release)),
            lambda value: value.update(qualification_components=CORE["python_qualification_components"](self.release)),
            lambda value: value["input_binding"].update(schema_version=True),
            lambda value: value["input_binding"].update(policy_sha256="0" * 64),
            lambda value: value["input_binding"].update(extra=True),
        ):
            harness = self.harness()
            mutate(harness.compile)
            with self.subTest(mutate=mutate):
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.validate_compile(harness)
                with self.assertRaises(RUNTIME["RuntimeError_"]):
                    RUNTIME["validate_compile_qualification_components"](harness.compile, harness.release)

    def test_scoped_zstd_qualification_does_not_load_legacy_renderer(self):
        # The absence path must reject unexpected zstd evidence even without a
        # release. The required path must use the externally bound component IDs.
        contract = CORE["ROW_CONTRACT"]["contract_for_version"]("3.9.25")
        build = self.directory / "build"
        target = self.directory / "target"
        modules = target / "lib-dynload"
        modules.mkdir(parents=True)
        value = QUALIFIER["zstd_compile_evidence"](contract, None, {"arch": "x86_64"}, fixtures.TARGET,
            build, target, modules, Path("/unused"), Path("/unused"), {})
        self.assertEqual(value, {"policy": "absent", "module": None, "builds": None})
        (modules / "_zstd.fake.so").write_bytes(b"fixture")
        with self.assertRaises(QUALIFIER["QualificationError"]):
            QUALIFIER["zstd_compile_evidence"](contract, None, {"arch": "x86_64"}, fixtures.TARGET,
                build, target, modules, Path("/unused"), Path("/unused"), {})
        (modules / "_zstd.fake.so").unlink()
        harness = self.harness(fixtures.ZSTD_VERSION)
        components = POLICY["from_release"](self.release, harness.version, "x86_64",
                                            CORE["render_component_documents"])["zstd_components"]
        relative = harness.compile["zstd"]["module"]["path"]
        module = target / relative
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_bytes(b"ELF audit fixture")
        for prefix, identity in ((build, "host"), (target, fixtures.TARGET)):
            path = prefix / ".crossforge/zstd-build.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            harness.write_json(path, harness.zstd_manifest(identity, "x86_64"))
        function = QUALIFIER["zstd_compile_evidence"]
        with mock.patch.dict(function.__globals__, {
            "expected_zstd_components": mock.Mock(side_effect=AssertionError("legacy renderer was used")),
            "audit_zstd_module": mock.Mock(return_value=copy.deepcopy(harness.compile["zstd"]["module"])),
        }):
            actual = function(harness.contract, None, QUALIFIER["TARGETS"][fixtures.TARGET], fixtures.TARGET,
                build, target, module.parent, Path("/unused"), Path("/unused"), harness.compile["elf_audit"],
                components=components)
        self.assertEqual(actual["policy"], "required")
        self.assertEqual(actual["builds"], harness.compile["zstd"]["builds"])


if __name__ == "__main__":
    unittest.main()

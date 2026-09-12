"""Scoped runtime/final contracts; all execution and ELF records below are fixtures."""

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_python_qualification as fixtures

ROOT = Path(__file__).resolve().parents[2]
FINALIZER = fixtures.FINALIZER
RUNTIME = fixtures.RUNTIME_RUNNER
POLICY = FINALIZER["POLICY"]
CORE = fixtures.QUALIFIER["release_components"]()
SHA = CORE["canonical_sha256"]


class PythonRuntimeComponentInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.components = ROOT / "config/generated/components"
        cls.release = fixtures.RELEASE_CONFIG
        cls.documents = CORE["render_component_documents"](cls.release)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def policy(self, version=fixtures.VERSION, arch="x86_64"):
        name = POLICY["component_name"](version, arch)
        return POLICY["load"](self.components, version, arch, SHA(self.documents[name]))

    def arguments(self, policy):
        return argparse.Namespace(release=None, qualification_components=self.components,
            qualification_component_sha256=policy["component"]["canonical_sha256"],
            target=policy["target"]["triple"], version=policy["version"])

    def compile_identity(self, policy):
        return {"qualification_schema_version": 5, "input_binding": POLICY["binding"](policy),
                "target": policy["target"]["triple"], "version": policy["version"],
                "adapter": policy["contract"]["adapter"], "report_kind": "crossforge-cpython-compile"}

    def harness(self, version=fixtures.VERSION):
        harness = fixtures.PythonQualificationTests()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        harness.reset_fixture(version)
        policy = self.policy(version)
        harness.compile.pop("release_sha256")
        harness.compile.pop("qualification_components")
        harness.compile.update(self.compile_identity(policy))
        harness.write_json(harness.compile_path, harness.compile)
        overlay = harness.clean["runtime"]["overlay_evidence"]
        overlay["schema_version"] = 2
        overlay["identity"].pop("release_sha256")
        overlay["identity"]["input_binding"] = policy["runtime_overlay_binding"]
        overlay["identity_sha256"] = SHA(overlay["identity"])
        harness.clean["runtime"]["identity_sha256"] = overlay["identity_sha256"]
        # The production input selector provides the new report identity. These
        # fixture conversions do not assert that any runtime was executed.
        identity = RUNTIME["runtime_inputs"](self.arguments(policy), harness.compile)["report_identity"]
        for report in (harness.locked, harness.clean):
            report.pop("release_sha256")
            report.update(identity)
        harness.refresh_runtime_bindings()
        return harness

    def finalize(self, harness, **overrides):
        arguments = dict(qualification_components=self.components,
            qualification_component_sha256=self.policy(harness.version)["component"]["canonical_sha256"],
            actual_elf_evidence=harness.actual_elf_evidence(), abi_context_override=harness.abi_context)
        arguments.update(overrides)
        return FINALIZER["finalize"](harness.compile_path, harness.locked_path, harness.clean_path, None,
                                      fixtures.TARGET, harness.version, **arguments)

    def validate(self, report, harness, release=None):
        return FINALIZER["validate_final_report"](report, harness.release if release is None else release,
            fixtures.TARGET, harness.version, harness.abi_context, harness.actual_elf_evidence(), True)

    def test_all_twelve_runtime_input_policies_match_independent_release_projection(self):
        for version in fixtures.IMPLEMENTED_VERSIONS:
            for arch in ("x86_64", "aarch64"):
                policy = self.policy(version, arch)
                with self.subTest(version=version, arch=arch):
                    inputs = RUNTIME["runtime_inputs"](self.arguments(policy), self.compile_identity(policy))
                    self.assertEqual(inputs["policy"], POLICY["from_release"](self.release, version, arch,
                                                                            CORE["render_component_documents"]))
                    self.assertIsNone(inputs["release"])
                    self.assertEqual(inputs["report_identity"], {"qualification_schema_version": 4,
                                                                 "input_binding": POLICY["binding"](policy)})
                    self.assertEqual(inputs["abi"], FINALIZER["ABI_CONTRACT"]["release_abi_inputs"](self.release, arch))
                    self.assertEqual(inputs["runtime_executor"]["kind"], "native" if arch == "x86_64" else "qemu")

    def test_six_scoped_final_producers_pass_full_release_consumers(self):
        for version in fixtures.IMPLEMENTED_VERSIONS:
            with self.subTest(version=version):
                harness = self.harness(version)
                report = self.finalize(harness)
                self.assertEqual(report["qualification_schema_version"], 5)
                self.assertEqual(report["input_binding"], POLICY["binding"](self.policy(version)))
                self.assertNotIn("release_sha256", report)
                self.assertNotIn("qualification_components", report)
                self.assertEqual(report["compile"], harness.compile)
                self.assertEqual(report["executions"], {"locked-sysroot": harness.locked, "clean-rocky": harness.clean})
                self.assertEqual(self.validate(report, harness), report)

    def test_legacy_runtime_input_selector_keeps_release_identity(self):
        harness = fixtures.PythonQualificationTests()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        args = self.arguments(self.policy())
        args.release = harness.release_path
        args.qualification_components = args.qualification_component_sha256 = None
        inputs = RUNTIME["runtime_inputs"](args, harness.compile)
        self.assertEqual(inputs["report_identity"], {"qualification_schema_version": 3,
                                                     "release_sha256": SHA(harness.release)})
        self.assertIsNone(inputs["policy"])
        harness.compile["release_sha256"] = "0" * 64
        with self.assertRaises(RUNTIME["RuntimeError_"]):
            RUNTIME["runtime_inputs"](args, harness.compile)

    def test_new_report_survives_only_unrelated_release_changes(self):
        harness = self.harness()
        report = self.finalize(harness)
        changed = copy.deepcopy(harness.release)
        changed["product"]["version"] = "0.1.1"
        changed["python"]["versions"][0]["source"]["sigstore"]["bundle_sha256"] = "0" * 64
        changed["qemu"]["executor"]["cpu"] = "unrelated-to-native-x86"
        self.assertEqual(self.validate(report, harness, changed), report)
        for mutate in (
            lambda r: r["python"]["versions"][4]["source"]["sigstore"].update(bundle_sha256="0" * 64),
            lambda r: r["abi"]["targets"]["x86_64"]["baseline"].update(canonical_sha256="0" * 64),
            lambda r: r["base_image"]["manifests"].update(amd64="sha256:" + "0" * 64),
        ):
            changed = copy.deepcopy(harness.release)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(FINALIZER["FinalizationError"]):
                self.validate(report, harness, changed)

    def test_inputs_reject_missing_mixed_wrong_pins_and_compile_identity(self):
        policy = self.policy()
        for mutate in (
            lambda a, c: setattr(a, "release", ROOT / "config/release.json"),
            lambda a, c: setattr(a, "qualification_components", None),
            lambda a, c: setattr(a, "qualification_component_sha256", None),
            lambda a, c: setattr(a, "qualification_component_sha256", "0" * 64),
            lambda a, c: c.update(qualification_schema_version=4),
            lambda a, c: c.update(qualification_schema_version=5.0),
            lambda a, c: c.update(release_sha256=SHA(self.release)),
            lambda a, c: c.update(qualification_components={}),
            lambda a, c: c.update(target="aarch64-unknown-linux-gnu"),
            lambda a, c: c.update(adapter="wrong"),
            lambda a, c: c["input_binding"].update(policy_sha256="0" * 64),
        ):
            args, compile_report = self.arguments(policy), self.compile_identity(policy)
            mutate(args, compile_report)
            with self.subTest(mutate=mutate), self.assertRaises(RUNTIME["RuntimeError_"]):
                RUNTIME["runtime_inputs"](args, compile_report)
        harness = self.harness()
        for overrides in ({"qualification_components": None}, {"qualification_component_sha256": None},
                          {"qualification_component_sha256": "0" * 64}):
            with self.subTest(overrides=overrides), self.assertRaises(FINALIZER["FinalizationError"]):
                self.finalize(harness, **overrides)

    def test_scoped_final_rejects_legacy_nested_reports_and_mixed_fields(self):
        harness = self.harness()
        original = self.finalize(harness)
        for node in ("final", "compile", "locked-sysroot", "clean-rocky"):
            for field in ("release_sha256", "qualification_schema_version", "input_binding"):
                report = copy.deepcopy(original)
                value = report if node == "final" else report["compile"] if node == "compile" else report["executions"][node]
                if field == "input_binding":
                    value[field]["schema_version"] = True
                elif field == "qualification_schema_version":
                    value[field] -= 1
                    value.pop("input_binding")
                    value["release_sha256"] = SHA(harness.release)
                    if node in ("final", "compile"):
                        value["qualification_components"] = CORE["python_qualification_components"](harness.release)
                else:
                    value[field] = SHA(harness.release)
                with self.subTest(node=node, field=field), self.assertRaises(FINALIZER["FinalizationError"]):
                    self.validate(report, harness)
        report = copy.deepcopy(original)
        overlay = report["executions"]["clean-rocky"]["runtime"]["overlay_evidence"]
        overlay["schema_version"] = 1
        overlay["identity"].pop("input_binding")
        overlay["identity"]["release_sha256"] = SHA(harness.release)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "legacy runtime overlay"):
            self.validate(report, harness)

    def test_artifacts_runtime_providers_probes_and_serialization_remain_required(self):
        harness = self.harness()
        original = self.finalize(harness)
        for mutate in (
            lambda r: r["compile"]["target_artifact_guard"].update(denied_execution_attempts=1),
            lambda r: r["compile"]["abi"]["baseline"].update(canonical_sha256="0" * 64),
            lambda r: r["executions"]["locked-sysroot"].update(compile_report_sha256="0" * 64),
            lambda r: r["executions"]["clean-rocky"]["runtime_providers"].update(policy_sha256="0" * 64),
            lambda r: r["executions"]["locked-sysroot"]["probe"].update(status="failed"),
            lambda r: r["executions"]["clean-rocky"]["executor"].update(kind="qemu"),
            lambda r: r["runtime_result_sha256"].update(**{"clean-rocky": "0" * 64}),
            lambda r: r["executions"]["clean-rocky"]["runtime"]["overlay_evidence"]["identity"]["base_image"].update(manifest_digest="sha256:" + "0" * 64),
        ):
            report = copy.deepcopy(original)
            mutate(report)
            with self.subTest(mutate=mutate), self.assertRaises(FINALIZER["FinalizationError"]):
                self.validate(report, harness)

    def test_arm_scoped_runtime_checks_exact_explicit_executor(self):
        harness = self.harness()
        policy = self.policy(arch="aarch64")
        target = policy["target"]["triple"]
        context = FINALIZER["policy_context"](policy)
        abi = FINALIZER["default_abi_context"](target)
        report = copy.deepcopy(harness.locked)
        report.update(target=target, input_binding=POLICY["binding"](policy))
        report["runtime"]["identity_sha256"] = context["sysroot_sha256"]
        report["runtime_providers"] = copy.deepcopy(abi["runtime_provider_evidence"])
        report["executor"] = dict(policy["runtime_executor"], kind="explicit-qemu")
        for name in ("probe", "device_probe"):
            report[name]["target"] = target
            report[name]["sysconfig"].update(arch="aarch64", host_gnu_type=target, multiarch="aarch64-linux-gnu",
                platform="linux-aarch64", prefix="/opt/crossforge/python/cp313/targets/" + target,
                soabi="cpython-313-aarch64-linux-gnu", ext_suffix=".cpython-313-aarch64-linux-gnu.so",
                cc="/opt/crossforge/targets/%s/bin/%s-gcc --sysroot=/opt/crossforge/sysroots/el8/aarch64" % (target, target))
        report["probe"]["extension"]["file"] = "_crossforge.cpython-313-aarch64-linux-gnu.so"
        # This isolates runtime validation; the compile fixture is not an ARM
        # compile qualification and is never submitted to the final validator.
        compile_report = {"qualification_schema_version": 5, "python_sha256": fixtures.PYTHON_SHA256,
                          "extension": {"sha256": fixtures.EXTENSION_SHA256}}
        args = ("locked-sysroot", context, compile_report, report["compile_report_sha256"], target, harness.version, abi)
        self.assertEqual(FINALIZER["validate_runtime_result"](report, *args), report)
        for key in ("binary_sha256", "version", "cpu", "uname_release", "kind"):
            changed = copy.deepcopy(report)
            changed["executor"][key] = "0" * 64 if key == "binary_sha256" else "wrong"
            with self.subTest(key=key), self.assertRaises(FINALIZER["FinalizationError"]):
                FINALIZER["validate_runtime_result"](changed, *args)

    def test_cropped_runtime_stage_produces_final_report_without_release_renderer(self):
        harness = self.harness(fixtures.ZSTD_VERSION)
        policy = self.policy(harness.version)
        scripts = self.directory / "scripts"
        scripts.mkdir()
        block = (ROOT / "docker/python.Dockerfile").read_text().split(" AS cpython-runtime-input\n", 1)[1].split("\nFROM ", 1)[0]
        names = ("run-cpython-runtime.py", "finalize-cpython-qualification.py", "loader_evidence.py", "abi_contract.py",
                 "python_abi_audit.py", "python_runtime_providers.py", "python_qualification_policy.py", "release_component.py",
                 "python_row_contract.py", "python_runtime_overlay.py", "python_zstd_evidence.py", "target_artifact_audit.py")
        for name in names:
            self.assertIn("scripts/" + name, block)
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        for forbidden in ("release.json", "release-components-core.py", "validate-release.py"):
            self.assertNotIn(forbidden, block)
            self.assertFalse((scripts / forbidden).exists())
        components = self.directory / "components"
        for name in (policy["component"]["component"], "implementation/python-cp314-qualification-policy"):
            path = components / (name + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.components / (name + ".json"), path)
        request = self.directory / "request.json"
        harness.write_json(request, {"compile": str(harness.compile_path), "locked": str(harness.locked_path),
            "clean": str(harness.clean_path), "abi": harness.abi_context, "elf": harness.actual_elf_evidence(),
            "components": str(components), "digest": policy["component"]["canonical_sha256"],
            "target": fixtures.TARGET, "version": harness.version})
        code = '''import argparse, json, runpy, sys
from pathlib import Path
p=Path(sys.argv[1]); a=json.loads(Path(sys.argv[2]).read_text())
r=runpy.run_path(str(p/'run-cpython-runtime.py')); f=runpy.run_path(str(p/'finalize-cpython-qualification.py'))
args=argparse.Namespace(release=None,qualification_components=Path(a['components']),qualification_component_sha256=a['digest'],target=a['target'],version=a['version'])
r['runtime_inputs'](args,json.loads(Path(a['compile']).read_text()))
value=f['finalize'](Path(a['compile']),Path(a['locked']),Path(a['clean']),None,a['target'],a['version'],actual_elf_evidence=a['elf'],abi_context_override=a['abi'],qualification_components=args.qualification_components,qualification_component_sha256=a['digest'])
assert f['release_components'].__globals__['RELEASE_COMPONENTS'] is None
assert r['release_components'].__globals__['RELEASE_COMPONENTS'] is None
print(json.dumps(value,sort_keys=True))
'''
        environment = dict(os.environ, PYTHONPATH=str(scripts))
        result = subprocess.run([sys.executable, "-c", code, str(scripts), str(request)], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.finalize(harness))
        command = [sys.executable, str(scripts / "python_qualification_policy.py"),
                   "--qualification-components", str(components), "--qualification-component-sha256", policy["component"]["canonical_sha256"],
                   "--row", "cp314", "--version", harness.version, "--adapter", policy["contract"]["adapter"], "--arch", "x86_64"]
        result = subprocess.run(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        command[command.index("--row") + 1] = "cp313"
        result = subprocess.run(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

"""Real report-policy checks with synthetic reports, without target execution."""

import copy
import contextlib
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

ROOT = Path(__file__).resolve().parents[2]
REPORT = runpy.run_path(str(ROOT / "scripts/toolchain_report.py"))
SDK = runpy.run_path(str(ROOT / "scripts/qualify-vcpkg-sdk.py"))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import bake_materials
finally:
    sys.path.pop(0)
COMPONENT = REPORT["POLICY"]["component"]
ARCHES = ("x86_64", "aarch64")
FILES = ["sources/gcc", "sources/binutils"] + ["%s/%s-%s" % (group, arch, suffix)
    for arch in ARCHES for group, suffix in (("toolchain", "qualification"), ("toolchain", "build"), ("abi", "baseline"))]


class VcpkgToolchainInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.components = self.root / "components"
        self.release = COMPONENT.load_json(ROOT / "config/release.json")
        self.release_sha = COMPONENT.canonical_sha256(self.release)
        self.pins = {}
        for name in FILES:
            path = self.components / (name + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "config/generated/components" / (name + ".json"), path)
        for arch in ARCHES:
            self.pins[arch] = COMPONENT.canonical_sha256(COMPONENT.load_json(
                self.components / ("toolchain/%s-qualification.json" % arch)))
        self.policies = {arch: REPORT["POLICY"]["load"](self.components, arch, self.pins[arch]) for arch in ARCHES}
        self.reports = self.root / "reports"
        self.reports.mkdir()
        for arch in ARCHES:
            self.write(arch, self.fixture(arch))
        (self.reports / "x86_64-clean-runtime.ok").write_bytes(b"passed\n")

    def fixture(self, arch):
        policy = self.policies[arch]
        value = {"target": policy["target"]["triple"], "sysroot_sha256": policy["target"]["sysroot"]["canonical_sha256"],
                 "compiler_version": policy["gcc"]["version"], "binutils_version": "GNU ld " + policy["binutils"]["version"],
                 "sources": {tool: policy[tool]["source"] for tool in ("gcc", "binutils")},
                 "qualification_component": policy["component"], "qualification_schema_version": 2,
                 "input_binding": REPORT["POLICY"]["binding"](policy), "report_kind": "crossforge-toolchain-qualification",
                 "runtime_base": policy["runtime_base"], "runtime_executor": policy["runtime_executor"],
                 "abi_baseline": policy["abi_baseline"], "locked_sysroot_execution": {"status": "passed"}}
        if arch == "aarch64":
            value["clean_runtime_execution"] = {"status": "passed"}
        return copy.deepcopy(value)

    def write(self, arch, report):
        path = self.reports / (arch + ".json")
        path.write_text(json.dumps(report))
        return path

    def qualify(self, module=SDK, directory=None, pins=None):
        return module["qualify_toolchain_reports"](self.release, self.release_sha, self.reports,
            directory or self.components, self.pins if pins is None else pins)

    def test_component_reader_and_full_release_adapter_agree_for_both_targets(self):
        expected = SDK["qualify_toolchain_reports"](self.release, self.release_sha, self.reports)
        self.assertEqual(self.qualify(), expected)
        for arch in ARCHES:
            self.assertEqual(REPORT["qualify_policy_toolchain_report"](self.reports / (arch + ".json"), self.policies[arch]),
                             expected[arch])

    def test_report_target_sources_sysroot_abi_and_runtime_checks_are_preserved(self):
        for arch in ARCHES:
            mutations = {"target": "wrong", "sysroot_sha256": "0" * 64, "sources": {}, "compiler_version": "0.0",
                         "binutils_version": "GNU ld 1.0", "qualification_component": {}, "input_binding": {},
                         "qualification_schema_version": True, "report_kind": "wrong", "runtime_base": {},
                         "runtime_executor": {}, "abi_baseline": {}, "locked_sysroot_execution": {"status": "failed"},
                         "release_sha256": self.release_sha}
            if arch == "aarch64":
                mutations["clean_runtime_execution"] = {"status": "failed"}
            for field, value in mutations.items():
                with self.subTest(arch=arch, field=field), self.assertRaises(REPORT["QualificationError"]):
                    REPORT["qualify_policy_toolchain_report"](self.write(arch, dict(self.fixture(arch), **{field: value})),
                                                               self.policies[arch])

    def test_component_entry_rejects_legacy_reports_without_relabeling_them(self):
        for arch in ARCHES:
            report = self.fixture(arch)
            report.pop("input_binding")
            report["release_sha256"] = self.release_sha
            if arch == "aarch64":
                report["qualification_schema_version"] = 1
            else:
                report.pop("qualification_schema_version")
            path = self.write(arch, report)
            before = path.read_bytes()
            with self.assertRaisesRegex(REPORT["QualificationError"], "requires a scoped"):
                REPORT["qualify_policy_toolchain_report"](path, self.policies[arch])
            policy = self.policies[arch]
            REPORT["qualify_prior_toolchain_report"](arch, policy["target"]["triple"], path, self.release,
                self.release_sha, policy["target"]["sysroot"]["canonical_sha256"], policy["component"])
            self.assertEqual(path.read_bytes(), before)

    def test_policy_entry_rejects_unknown_fields_schema_and_malformed_expected_policy(self):
        for value in (None, {}, dict(self.policies["x86_64"], extra="unknown"),
                      dict(self.policies["x86_64"], schema_version=True), dict(self.policies["x86_64"], target=None)):
            with self.subTest(value=value), self.assertRaises(REPORT["QualificationError"]):
                REPORT["qualify_policy_toolchain_report"](self.reports / "x86_64.json", value)

    def test_new_entry_preserves_clean_marker_and_safe_report_requirements(self):
        marker = self.reports / "x86_64-clean-runtime.ok"
        marker.write_bytes(b"failed\n")
        with self.assertRaises(REPORT["QualificationError"]):
            REPORT["qualify_policy_toolchain_report"](self.reports / "x86_64.json", self.policies["x86_64"])
        marker.unlink()
        (self.root / "marker").write_bytes(b"passed\n")
        marker.symlink_to(self.root / "marker")
        with self.assertRaises(REPORT["QualificationError"]):
            REPORT["qualify_policy_toolchain_report"](self.reports / "x86_64.json", self.policies["x86_64"])
        path = self.reports / "aarch64.json"
        path.rename(self.root / "arm.json")
        path.symlink_to(self.root / "arm.json")
        with self.assertRaises(REPORT["QualificationError"]):
            REPORT["qualify_policy_toolchain_report"](path, self.policies["aarch64"])

    def test_pins_must_cover_exactly_both_architectures(self):
        for pins in ({}, {"x86_64": self.pins["x86_64"]}, dict(self.pins, surprise="0" * 64),
                     dict(self.pins, aarch64="0" * 64)):
            with self.subTest(pins=pins), self.assertRaises(SDK["QualificationError"]):
                self.qualify(pins=pins)
        for directory, pins in ((self.components, None), (None, self.pins)):
            with self.assertRaises(SDK["QualificationError"]):
                SDK["qualify_toolchain_reports"](self.release, self.release_sha, self.reports, directory, pins)

    def test_changed_transitive_source_or_missing_abi_projection_fails(self):
        path = self.components / "sources/gcc.json"
        original = path.read_bytes()
        document = COMPONENT.load_json(path)
        document["materials"][0]["value"] = "changed"
        path.write_text(json.dumps(document))
        with self.assertRaises(SDK["QualificationError"]):
            self.qualify()
        path.write_bytes(original)
        (self.components / "abi/aarch64-baseline.json").unlink()
        with self.assertRaises(SDK["QualificationError"]):
            self.qualify()

    def test_vcpkg_keeps_complete_release_expectations_for_its_existing_report(self):
        before = self.qualify()
        self.release["python"]["versions"][0]["patches"][0]["sha256"] = "0" * 64
        self.release_sha = COMPONENT.canonical_sha256(self.release)
        self.assertEqual(self.qualify(), before)
        for field in (("gts", "gcc_version"), ("qemu", "executor", "binary_sha256")):
            original = copy.deepcopy(self.release)
            value = self.release
            for key in field[:-1]:
                value = value[key]
            value[field[-1]] = "0.0" if field[-1] == "gcc_version" else "0" * 64
            with self.subTest(field=field), self.assertRaises(SDK["QualificationError"]):
                self.qualify()
            self.release = original

    def test_trimmed_sdk_scripts_accept_reports_without_loading_a_renderer(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        expected_scripts = {"qualify-vcpkg-sdk.py", "fetch-vcpkg-history.py", "release_component.py",
                            "toolchain_report.py", "toolchain_policy.py", "vcpkg_policy.py"}
        for name in expected_scripts:
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        self.assertEqual({path.name for path in scripts.iterdir()}, expected_scripts)
        trimmed = runpy.run_path(str(scripts / "qualify-vcpkg-sdk.py"))
        self.assertEqual(self.qualify(module=trimmed), self.qualify())
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        for write_bytecode in (False, True):
            with self.subTest(write_bytecode=write_bytecode):
                flags = [] if write_bytecode else ["-B"]
                result = subprocess.run([sys.executable] + flags +
                    [str(scripts / "qualify-vcpkg-sdk.py"), "--help"], env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"--toolchain-components", result.stdout)
                self.assertEqual({path.name for path in scripts.iterdir()} - {"__pycache__"}, expected_scripts)
                if write_bytecode:
                    self.assertTrue((scripts / "__pycache__").is_dir())
        self.assertEqual(len(list(self.components.rglob("*.json"))), 8)

    def cli(self):
        argv = ["qualify-vcpkg-sdk.py"]
        for name in ("release", "root", "source-manifest", "integration-manifest", "cmake-root", "triplet-root",
                     "qemu", "ninja-report", "cmake-report"):
            argv += ["--" + name, str(self.root / name)]
        for role in ("source", "integration", "sdk", "ninja", "cmake"):
            argv += ["--%s-component" % role, str(self.root / role), "--%s-component-sha256" % role, "0" * 64]
        return argv + ["--output", str(self.root / "output.json")]

    def test_cli_forwards_both_component_pins_and_retains_legacy_mode(self):
        qualifier = mock.Mock(return_value={"status": "fixture"})
        extra = ["--toolchain-components", str(self.components)]
        for arch in ARCHES:
            extra += ["--toolchain-%s-component-sha256" % arch, self.pins[arch]]
        for suffix in ([], extra):
            with mock.patch.object(sys, "argv", self.cli() + suffix), contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.dict(SDK["main"].__globals__, {"qualify": qualifier}):
                self.assertEqual(SDK["main"](), 0)
            self.assertEqual(qualifier.call_args[0][-2:], (self.components, self.pins) if suffix else (None, None))

    def test_cli_rejects_incomplete_component_options_before_qualification(self):
        for extra in (["--toolchain-components", str(self.components)],
                      ["--toolchain-x86_64-component-sha256", self.pins["x86_64"]],
                      ["--toolchain-components", str(self.components), "--toolchain-aarch64-component-sha256", self.pins["aarch64"]]):
            qualifier = mock.Mock()
            with mock.patch.object(sys, "argv", self.cli() + extra), \
                 mock.patch.dict(SDK["main"].__globals__, {"qualify": qualifier}), self.assertRaises(SDK["QualificationError"]):
                SDK["main"]()
            qualifier.assert_not_called()
            self.assertFalse((self.root / "output.json").exists())


class VcpkgToolchainGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker Buildx is required for canonical graph checks")
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "-f", "docker-bake.hcl",
            "-f", "docker-bake.override.json", "--print", "sdk-phase13-base"], cwd=str(ROOT)))

    def test_sdk_graph_binds_both_toolchain_policy_pins(self):
        definition = self.graph["target"]["sdk-phase13-base"]
        for arch in ARCHES:
            document = COMPONENT.load_json(ROOT / "config/generated/components/toolchain" / (arch + "-qualification.json"))
            self.assertEqual(definition["args"]["TOOLCHAIN_%s_QUALIFICATION_COMPONENT_SHA256" % arch.upper()],
                             COMPONENT.canonical_sha256(document))
        value = bake_materials.capture(ROOT, self.graph, "sdk-phase13-base", "vcpkg/sdk-build", "qualification",
                                      [arch + "-unknown-linux-gnu" for arch in ARCHES], {"fixture": "graph only"})
        files = {record["path"] for record in value["files"]}
        self.assertTrue({"config/generated/components/" + name + ".json" for name in FILES} <= files)
        self.assertNotIn("scripts/release-components-core.py", files)
        self.assertNotIn("scripts/python_row_contract.py", files)
        self.assertNotIn("config/release.json", files)

    def test_sdk_stage_copies_exactly_six_runtime_modules(self):
        _, stages, _ = bake_materials.recipe((ROOT / "docker/vcpkg.Dockerfile").read_text())
        instructions = stages["vcpkg-sdk-base"]["instructions"]
        copies = [source for line in instructions if line.startswith("COPY ")
                  for source in bake_materials._copy(line)[1] if source.startswith("scripts/")]
        self.assertEqual(set(copies), {"scripts/" + name for name in (
            "fetch-vcpkg-history.py", "release_component.py", "qualify-vcpkg-sdk.py", "toolchain_policy.py", "toolchain_report.py", "vcpkg_policy.py")})
        runs = [line for line in instructions if line.startswith("RUN ")]
        self.assertEqual(len(runs), 1)
        self.assertIn("--components /opt/crossforge/qualification/vcpkg/inputs", runs[0])
        for arch in ARCHES:
            self.assertIn("--toolchain-%s-component-sha256" % arch, runs[0])


if __name__ == "__main__":
    unittest.main()

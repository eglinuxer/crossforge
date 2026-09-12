"""GCC qualification reads only authenticated GCC policy, preserving its gates."""

import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
POLICY = runpy.run_path(str(ROOT / "scripts/gcc_testsuite_policy.py"))
CONTRACT = runpy.run_path(str(ROOT / "scripts/validate-gcc-testsuite.py"))
RUNNER = runpy.run_path(str(ROOT / "scripts/run-gcc-testsuite.py"))
COMPONENT = POLICY["COMPONENT"]
NAME = POLICY["COMPONENT_NAME"]
DIRECTORY = ROOT / "config/generated/components"
DOCUMENT = COMPONENT["load_json"](DIRECTORY / (NAME + ".json"))
DIGEST = COMPONENT["canonical_sha256"](DOCUMENT)
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_qualification, component_inputs
finally:
    sys.path.pop(0)


class GccPolicyComponentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.components = self.root / "config/generated/components"
        for name in (NAME, "sources/gcc", "toolchain/aarch64-qualification"):
            target = self.components / (name + ".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DIRECTORY / (name + ".json"), target)

    def write_root(self, document):
        (self.components / (NAME + ".json")).write_text(json.dumps(document))
        return COMPONENT["canonical_sha256"](document)

    def test_scoped_policy_matches_validated_release_for_both_targets(self):
        release = CONTRACT["validate_release_contract"](ROOT / "config/release.json")["release"]
        for arch in ("x86_64", "aarch64"):
            expected = POLICY["from_release"](release, arch, {"component": NAME, "canonical_sha256": DIGEST})
            actual = POLICY["load"](self.components, arch, DIGEST)
            self.assertEqual(actual, expected)
        (self.components / "toolchain/aarch64-qualification.json").unlink()
        self.assertEqual(POLICY["load"](self.components, "x86_64", DIGEST)["executor"], {"kind": "native"})
        with self.assertRaises(POLICY["PolicyError"]):
            POLICY["load"](self.components, "aarch64", DIGEST)

    def test_minimal_workspace_runs_all_existing_plan_and_baseline_checks_without_release(self):
        for name in ("validate-gcc-testsuite.py", "validate-release.py", "release_component.py", "gcc_testsuite_policy.py"):
            path = self.root / "scripts" / name
            path.parent.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / "scripts" / name, path)
        for path in ("config/gcc-testsuite-smoke.json", "config/gcc-testsuite-full.json",
                     "config/schemas/gcc-testsuite-plan.schema.json", "config/schemas/gcc-testsuite-baseline.schema.json"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
        for path in ("tests/gcc", "patches/gcc"):
            shutil.copytree(ROOT / path, self.root / path)
        self.assertFalse((self.root / "config/release.json").exists())
        self.assertFalse((self.root / "config/schemas/release.schema.json").exists())
        isolated = runpy.run_path(str(self.root / "scripts/validate-gcc-testsuite.py"))
        actual = isolated["validate_component_contract"](self.components, "x86_64", DIGEST)
        expected = CONTRACT["validate_release_contract"](ROOT / "config/release.json")
        for profile in ("smoke", "full"):
            found, prior = actual["profiles"][profile], expected["profiles"][profile]
            self.assertEqual(found["plan"], prior["plan"])
            self.assertEqual(found["plan_sha256"], prior["plan_sha256"])
            self.assertEqual({key: value["document"] for key, value in found["baselines"].items()},
                             {key: value["document"] for key, value in prior["baselines"].items()})
        path = self.root / "tests/gcc/baselines/full/x86_64-host-direct.json"
        baseline = json.loads(path.read_text())
        baseline["unexpected"] = baseline["unexpected"][1:]
        path.write_text(json.dumps(baseline))
        with self.assertRaisesRegex(isolated["ValidationError"], "baseline digest differs"):
            isolated["validate_component_contract"](self.components, "x86_64", DIGEST)

    def test_bad_material_sets_dependencies_types_and_unknown_fields_fail(self):
        mutations = []
        changed = copy.deepcopy(DOCUMENT)
        changed["materials"].pop()
        mutations.append(changed)
        changed = copy.deepcopy(DOCUMENT)
        changed["materials"].append({"path": "/surprise", "value": "extra"})
        mutations.append(changed)
        changed = copy.deepcopy(DOCUMENT)
        changed["materials"][0]["value"] = True
        mutations.append(changed)
        changed = copy.deepcopy(DOCUMENT)
        changed["dependencies"].pop()
        mutations.append(changed)
        changed = copy.deepcopy(DOCUMENT)
        changed["schema_version"] = True
        mutations.append(changed)
        changed = copy.deepcopy(DOCUMENT)
        changed["extra"] = "unknown"
        mutations.append(changed)
        for document in mutations:
            with self.subTest(document=document):
                digest = self.write_root(document)
                with self.assertRaises(POLICY["PolicyError"]):
                    POLICY["load"](self.components, "x86_64", digest)
        self.write_root(DOCUMENT)
        with self.assertRaises(POLICY["PolicyError"]):
            POLICY["load"](self.components, "x86_64", "0" * 64)
        source = self.components / "sources/gcc.json"
        value = json.loads(source.read_text())
        value["materials"][0]["value"] = "tampered"
        source.write_text(json.dumps(value))
        with self.assertRaises(POLICY["PolicyError"]):
            POLICY["load"](self.components, "x86_64", DIGEST)

    def test_runner_component_path_reaches_compiler_checks_without_full_release_loading(self):
        argv = ["runner", "--components", str(DIRECTORY), "--qualification-component", str(DIRECTORY / (NAME + ".json")),
                "--qualification-component-sha256", DIGEST, "--target", "x86_64-unknown-linux-gnu",
                "--runtime-tier", "host-direct"]
        for name in ("build", "source", "prefix", "sysroot", "site", "host-marker", "output", "report"):
            argv += ["--" + name, str(self.root / name)]
        globals_ = RUNNER["main"].__globals__
        stub = mock.Mock(side_effect=RUNNER["ValidationError"]("reached compiler verification"))
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(globals_, {"require_file": stub}), \
             mock.patch.dict(globals_["CONTRACT"], {"validate_release_contract": mock.Mock(side_effect=AssertionError("full release read"))}), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(RUNNER["main"](), 1)
        self.assertIn("reached compiler verification", errors.getvalue())
        self.assertEqual(stub.call_args[0][1], "final GCC")
        with mock.patch.object(sys, "argv", argv + ["--mode", "observation"]), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(RUNNER["main"](), 1)
        self.assertIn("observation mode requires the complete release input", errors.getvalue())


class GccPolicyGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker Buildx is required for canonical graph checks")
        cls.settings = [component_qualification.spec(arch, profile) for arch, profile in (
            ("x86_64", "gcc-smoke"), ("x86_64", "gcc-full"), ("aarch64", "gcc-smoke"))]
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "-f", "docker-bake.hcl",
            "-f", "docker-bake.override.json", "--print"] + [value["target"] for value in cls.settings], cwd=str(ROOT)))

    def test_all_gcc_gates_exclude_full_release_and_source_compilers_but_keep_policy_and_baselines(self):
        for settings in self.settings:
            graph, bindings = copy.deepcopy(self.graph), {}
            for index, (context, role) in enumerate(sorted(settings["contexts"].items()), 1):
                digest = "sha256:" + str(index) * 64
                for definition in graph["target"].values():
                    if context in definition.get("contexts", {}):
                        definition["contexts"][context] = "oci-layout:///graph-fixture@" + digest
                bindings[context] = {"component": "toolchain/" + settings["arch"] + (
                    "-install" if role == "toolchain-install" else "-gcc-test-context"),
                    "inputs_sha256": str(index) * 64, "artifact_digest": digest}
            captured = component_qualification.qualification_inputs(ROOT, graph, settings,
                {"build": {"identity": "graph-fixture"}, "host": {"identity": "graph-fixture"}}, bindings)
            paths = {record["path"] for record in captured["files"]}
            with self.subTest(settings=settings):
                self.assertFalse(paths & {"config/release.json", "config/schemas/release.schema.json", "scripts/build-gcc.sh"})
                self.assertTrue({"scripts/gcc_testsuite_policy.py", "scripts/run-gcc-testsuite.py",
                    "config/gcc-testsuite-smoke.json", "config/gcc-testsuite-full.json",
                    "tests/gcc/baselines/full/x86_64-host-direct.json"} <= paths)
                self.assertEqual(captured["parameters"]["required_runs"], {
                    settings["stages"][0]: 1, settings["stages"][1]: 2 if settings["arch"] == "aarch64" else 1})


if __name__ == "__main__":
    unittest.main()

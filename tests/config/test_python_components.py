"""Real Bake closures must preserve independent targets and exclude compilers at handoff."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import python_components as python, bake_materials, component_inputs
    from crossforge_internal.identity import IdentityError
finally:
    sys.path.pop(0)


class PythonComponentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "--print",
            "python-row-cp39", "python-row-cp314", "toolchain-x86_64-build-export", "toolchain-aarch64-build-export"], cwd=ROOT))
        cls.execution = {"buildkit_image": "moby/buildkit:test@sha256:" + "a" * 64}

    def verified(self, subject, expected, role, target, *args):
        # Only OCI transport is stubbed; specs and every material closure use
        # the actual repository graph and the actual producer input functions.
        self.assertEqual(role, expected["parameters"]["role"])
        digest = "sha256:" + hashlib.sha256(expected["component"].encode()).hexdigest()
        return "oci-layout:///fixture@" + digest, {"component": expected["component"],
            "inputs_sha256": component_inputs.identity(expected), "artifact_digest": digest}

    def test_exports_are_distinct_and_no_install_contains_test_tree(self):
        native = python.spec(ROOT, "cp39", "build", "install")
        self.assertEqual(native["version"], "3.9.25")
        self.assertEqual(native["targets"], ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"])
        identities = {native["component"]}
        for arch in python.ARCHES:
            install = python.spec(ROOT, "cp39", arch, "install")
            context = python.spec(ROOT, "cp39", arch, "test-context")
            identities.update((install["component"], context["component"]))
            self.assertEqual(install["copies"], ["/opt/crossforge/python/cp39/targets/%s-unknown-linux-gnu/" % arch])
            self.assertTrue(all(path.startswith("/work/") for path in context["copies"]))
            self.assertNotEqual(install["role"], context["role"])
        self.assertEqual(len(identities), 5)
        for row, arch, kind in (("cp38", "build", "install"), ("cp39", "build", "test-context"),
                                 ("cp39", "armv7", "install"), ("cp39", "x86_64", "qualified")):
            with self.assertRaises(ValueError):
                python.spec(ROOT, row, arch, kind)

    def test_cross_build_only_binds_its_own_toolchain_and_build_python(self):
        settings = python.spec(ROOT, "cp39", "aarch64", "install")
        with mock.patch.object(python, "verify", side_effect=self.verified):
            resolved, bindings = python.bind_build(ROOT, self.graph, settings, self.execution,
                {"toolchain-install": {}, "build-python": {}}, "fixture")
        self.assertEqual(resolved["target"]["cpython-cross-cp39-x86_64"], self.graph["target"]["cpython-cross-cp39-x86_64"])
        inputs = python.inputs(ROOT, resolved, settings, self.execution, bindings)
        paths = {record["path"] for record in inputs["files"]}
        self.assertIn("scripts/build-cpython-cross.sh", paths)
        self.assertFalse(paths & {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "config/release.json"})
        self.assertEqual(len(inputs["dependencies"]), 2)
        changed = copy.deepcopy(bindings)
        changed[next(iter(changed))]["inputs_sha256"] = "0" * 64
        self.assertNotEqual(inputs, python.inputs(ROOT, resolved, settings, self.execution, changed))

    def test_complete_row_materials_end_at_all_seven_verified_subjects(self):
        subjects = {"build": {}}
        subjects.update({arch + "-" + kind: {} for arch in python.ARCHES for kind in ("install", "test-context", "toolchain")})
        with mock.patch.object(python, "verify", side_effect=self.verified):
            resolved, bindings = python.bind_row(ROOT, self.graph, "cp39", self.execution, subjects, "fixture")
        inputs = bake_materials.capture(ROOT, resolved, "python-row-cp39", "qualification/python-cp39", "qualification",
            ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"], {"build": self.execution}, artifacts=bindings)
        self.assertEqual(len(inputs["dependencies"]), 7)
        paths = {record["path"] for record in inputs["files"]}
        self.assertFalse(paths & {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"})
        self.assertTrue({"scripts/qualify-cpython.py", "scripts/finalize-cpython-qualification.py",
                         "docker/finalize-python-row.py", "tests/python/runtime_probe.py", "config/release.json"} <= paths)
        for arch in python.ARCHES:
            self.assertIn("cpython-cp39-%s-qualify" % arch, inputs["parameters"]["recipes"])

    def test_private_zstd_consumes_the_same_verified_toolchain_without_gcc_sources(self):
        for arch in python.ARCHES:
            settings = python.spec(ROOT, "cp314", arch, "install")
            with mock.patch.object(python, "verify", side_effect=self.verified):
                resolved, bindings = python.bind_build(ROOT, self.graph, settings, self.execution,
                    {"toolchain-install": {}, "build-python": {}}, "fixture")
            inputs = python.inputs(ROOT, resolved, settings, self.execution, bindings)
            paths = {record["path"] for record in inputs["files"]}
            self.assertIn("scripts/build-zstd.sh", paths)
            self.assertFalse(paths & {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh"})
            cross = "cpython-cross-cp314-" + arch
            zstd = "zstd-%s-build" % arch
            self.assertEqual(resolved["target"][cross]["contexts"]["crossforge_toolchain"],
                             resolved["target"][zstd]["contexts"]["crossforge_toolchain"])
            self.assertEqual(len(inputs["dependencies"]), 2)
            other = "zstd-%s-build" % ("aarch64" if arch == "x86_64" else "x86_64")
            self.assertEqual(resolved["target"][other], self.graph["target"][other])
            bad = copy.deepcopy(self.graph)
            bad["target"][cross]["contexts"]["crossforge_zstd"] = "target:" + other
            with self.assertRaisesRegex(IdentityError, "zstd boundary differs"):
                python.bind_build(ROOT, bad, settings, self.execution,
                    {"toolchain-install": {}, "build-python": {}}, "fixture")

    def test_wrong_subject_set_stage_and_row_fail_before_production(self):
        settings = python.spec(ROOT, "cp39", "x86_64", "install")
        with mock.patch.object(python, "verify") as verify:
            for subjects in ({}, {"toolchain-install": {}}, {"toolchain-install": {}, "build-python": {}, "extra": {}}):
                with self.assertRaises(IdentityError):
                    python.bind_build(ROOT, self.graph, settings, self.execution, subjects, "fixture")
            with self.assertRaises(IdentityError):
                python.bind_row(ROOT, self.graph, "cp39", self.execution, {}, "fixture")
            verify.assert_not_called()
        for key, value in (("target", "cpython-row-export"), ("dockerfile", "docker/Dockerfile")):
            graph = copy.deepcopy(self.graph)
            graph["target"][settings["target"]][key] = value
            with self.assertRaises(IdentityError):
                python.inputs(ROOT, graph, settings, self.execution)
        graph = copy.deepcopy(self.graph)
        graph["target"][settings["target"]]["args"]["CPYTHON_ROW"] = "cp310"
        with self.assertRaises(IdentityError):
            python.inputs(ROOT, graph, settings, self.execution)


if __name__ == "__main__":
    unittest.main()

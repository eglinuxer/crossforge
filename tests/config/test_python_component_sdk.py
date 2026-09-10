"""The full SDK consumer must end at all six verified rows and both toolchains."""

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
    from crossforge_internal import python_sdk as sdk, python_components, component_build, component_inputs
    from crossforge_internal.identity import IdentityError
finally:
    sys.path.pop(0)


class PythonComponentSdkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "--print", "python-dev", "sdk-complete-dev"], cwd=ROOT))

    def setUp(self):
        self.execution = {"build": {"buildkit_image": "moby/buildkit:test@sha256:" + "a" * 64}, "host": {"kernel": "fixture"}}
        self.components = {"toolchains": {arch: {"receipt": arch, "receipt_sha256": "b" * 64, "layout": "/fixture"}
                                          for arch in python_components.ARCHES}, "rows": {}}
        for row in sdk.matrix(ROOT):
            subjects = {"build": {}}
            subjects.update({arch + "-" + kind: {} for arch in python_components.ARCHES for kind in ("install", "test-context")})
            subjects.update({arch + "-toolchain": dict(self.components["toolchains"][arch]) for arch in python_components.ARCHES})
            self.components["rows"][row] = {"subjects": subjects,
                "qualification": {"receipt": row, "receipt_sha256": "c" * 64, "layout": "/fixture"}}

    def reference(self, component):
        return "oci-layout:///fixture@sha256:" + hashlib.sha256(component.encode()).hexdigest()

    def verified(self, subject, expected, role, target, *args):
        reference = self.reference(expected["component"])
        return reference, {"component": expected["component"], "inputs_sha256": component_inputs.identity(expected),
                           "artifact_digest": reference.rsplit("@", 1)[1]}

    def receipt(self, path):
        name = str(path)
        component = "toolchain/%s-install" % name if name in python_components.ARCHES else "qualification/python-" + name
        return {"artifact": {"platform_digest": self.reference(component).rsplit("@", 1)[1]}}

    def bind(self, root="python-dev", graph=None, components=None):
        def qualification(receipt, trusted, expected, *args):
            return {"mode": "verified-prior-execution", "reference": self.reference(expected["component"]),
                    "qualification": {"producer": {"invocation": "urn:crossforge:local:original"}}}
        with mock.patch.object(python_components, "verify", side_effect=self.verified), \
             mock.patch.object(component_build, "verify_local", side_effect=lambda receipt, trusted, expected, *args: self.reference(expected["component"])), \
             mock.patch.object(sdk.component_qualification, "load_json", side_effect=self.receipt), \
             mock.patch.object(sdk, "load_json", side_effect=self.receipt), \
             mock.patch.object(sdk.python_qualification, "verify_local", side_effect=qualification):
            return sdk.bind(ROOT, graph or self.graph, root, self.execution, components or self.components, "fixture")

    def test_all_six_rows_and_both_toolchains_end_the_compiler_graph(self):
        resolved, bindings, reused = self.bind()
        captured = sdk.inputs(ROOT, resolved, "python-dev", self.execution, bindings)
        self.assertEqual(len(captured["dependencies"]), 8)
        self.assertEqual(len(captured["parameters"]["bake_targets"]), 12)
        self.assertEqual(captured["parameters"]["required_runs"], {"python-sdk-append": 12, "python-sdk-final": 1})
        self.assertEqual(set(reused), set(sdk.matrix(ROOT)))
        self.assertTrue(all(value["mode"] == "verified-prior-execution" for value in reused.values()))
        paths = {record["path"] for record in captured["files"]}
        self.assertIn("scripts/qualify-final-sdk.py", paths)
        self.assertFalse(paths & {"scripts/build-gcc.sh", "scripts/build-cpython-native.sh", "scripts/build-cpython-cross.sh"})

    def test_complete_sdk_keeps_packaging_and_consumer_gates_without_source_compilers(self):
        resolved, bindings, reused = self.bind("sdk-complete-dev")
        captured = sdk.inputs(ROOT, resolved, "sdk-complete-dev", self.execution, bindings)
        self.assertEqual(captured["parameters"]["required_runs"], {
            "python-sdk-append": 12, "python-sdk-final": 1, "sdk-complete-dev": 1})
        paths = {record["path"] for record in captured["files"]}
        self.assertTrue({"scripts/qualify-complete-sdk.py", "tools/crossforge/crosspack.py", "scripts/qualify-vcpkg-sdk.py"} <= paths)
        self.assertEqual(len(reused), 6)

    def test_missing_extra_or_mixed_rows_fail_before_verification(self):
        for change in ("missing", "extra", "mixed-toolchain"):
            components = copy.deepcopy(self.components)
            if change == "missing":components["rows"].pop("cp39")
            elif change == "extra":components["rows"]["cp38"] = components["rows"]["cp39"]
            else:components["rows"]["cp39"]["subjects"]["aarch64-toolchain"]["receipt_sha256"] = "0" * 64
            with mock.patch.object(sdk.component_qualification, "bind_subjects") as verify:
                with self.subTest(change=change), self.assertRaises(IdentityError):
                    sdk.bind(ROOT, self.graph, "python-dev", self.execution, components, "fixture")
                verify.assert_not_called()

    def test_wrong_order_row_version_chain_and_source_boundary_are_rejected(self):
        for target, field, key, value in (
            ("python-dev", "args", "CROSSFORGE_PYTHON_ROWS", "cp39"),
            ("python-dev-append-cp39", "args", "CPYTHON_VERSION", "3.9.24"),
            ("python-dev-append-cp39", "contexts", "crossforge_sdk_base", "target:sdk-toolchains-dev"),
            ("python-dev-append-cp314", "contexts", "crossforge_python_row", "target:python-row-cp313")):
            graph = copy.deepcopy(self.graph)
            graph["target"][target][field][key] = value
            with self.subTest(target=target, key=key), self.assertRaises(IdentityError):
                self.bind(graph=graph)


if __name__ == "__main__":
    unittest.main()

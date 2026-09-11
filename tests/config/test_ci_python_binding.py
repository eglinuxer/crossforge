"""Only authenticated raw Python artifacts replace their exact graph boundaries."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_build, component_resolution as resolution, python_handoff
    from crossforge_internal.identity import IdentityError, load_json
finally:
    sys.path.pop(0)


class PythonBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.specs = python_handoff.specs(ROOT, "cp39")
        self.graph = {"target": {"consumer": {"contexts": {}}, "unrelated": {"contexts": {"ordinary": "target:other"}}, "other": {}}}
        for name, settings in self.specs.items():
            self.graph["target"][settings["target"]] = {"dockerfile": "docker/python.Dockerfile", "target": settings["stage"]}
            self.graph["target"]["consumer"]["contexts"][name] = "target:" + settings["target"]
        self.graph["target"]["second"] = {"contexts": {"native": "target:" + self.specs["build"]["target"]}}
        self.toolchains = {arch + "-toolchain-install": {"status": "verified-build-component",
            "subject": {"receipt": arch + "-receipt", "receipt_sha256": "a" * 64, "layout": arch + "-layout"}}
            for arch in ("x86_64", "aarch64")}

    def resolve(self, source, graph, row, arch, kind, execution, subjects, cosign, directory, *args):
        self.assertEqual(graph, self.graph)
        name = "build" if arch == "build" else arch + "-" + kind
        if arch != "build":
            self.assertEqual(subjects["toolchain-install"], self.toolchains[arch + "-toolchain-install"]["subject"])
            self.assertEqual(subjects["build-python"]["receipt"], "prior-build")
        component_build.write_json(directory / "inputs.json", {"fixture": name})
        (directory / "oci").mkdir()
        (directory / "oci/blob").write_text("never upload")
        result = {"status": "verified-build-component", "context": "oci-layout:///fixture/" + name + "@sha256:" + "b" * 64,
                  "subject": {"receipt": "prior-" + name, "receipt_sha256": "a" * 64, "layout": "fixture"}}
        component_build.write_json(directory / "resolution.json", result)
        return result

    def bind(self, resolved=None, evidence=None):
        return resolution.bind_python(ROOT, self.graph, resolved or self.graph, self.toolchains, {}, Path("cosign"),
            self.root / "data", evidence or self.root / "evidence", "builder", Path("oras"))

    def test_all_edges_bind_once_in_dependency_order_and_do_not_mutate_source(self):
        original = copy.deepcopy(self.graph)
        with mock.patch.object(resolution, "python", side_effect=self.resolve) as resolve:
            graph, result = self.bind()
        self.assertEqual(self.graph, original)
        self.assertEqual(resolve.call_count, 5)
        self.assertEqual(resolve.call_args_list[0][0][3:5], ("build", "install"))
        self.assertEqual(graph["target"]["second"]["contexts"]["native"], graph["target"]["consumer"]["contexts"]["build"])
        self.assertTrue(all(value.startswith("oci-layout://") for value in graph["target"]["consumer"]["contexts"].values()))
        self.assertEqual(graph["target"]["unrelated"], original["target"]["unrelated"])
        self.assertEqual(result["required_producers"], [])
        self.assertFalse(any(path.name in ("oci", "blob") for path in (self.root / "evidence").rglob("*")))

    def test_missing_build_python_does_not_probe_or_accept_dependent_parts(self):
        with mock.patch.object(resolution, "python", return_value={"status": "build-required"}) as resolve:
            graph, result = self.bind()
        resolve.assert_called_once()
        self.assertEqual(graph, self.graph)
        self.assertEqual(result["required_producers"], sorted(settings["target"] for settings in self.specs.values()))
        for name, value in result["components"].items():
            self.assertNotIn("subject", value)
            self.assertEqual(value["status"], "build-required" if name == "cp39-build" else "dependency-build-required")

    def test_missing_one_toolchain_keeps_other_target_independent(self):
        self.toolchains["aarch64-toolchain-install"] = {"status": "build-required"}
        with mock.patch.object(resolution, "python", side_effect=self.resolve) as resolve:
            graph, result = self.bind()
        self.assertEqual(resolve.call_count, 3)
        self.assertTrue(graph["target"]["consumer"]["contexts"]["x86_64-install"].startswith("oci-layout://"))
        self.assertEqual(graph["target"]["consumer"]["contexts"]["aarch64-install"], "target:" + self.specs["aarch64-install"]["target"])
        self.assertEqual(result["required_producers"], sorted(self.specs[name]["target"] for name in ("aarch64-install", "aarch64-test-context")))

    def test_unknown_or_changed_producer_boundaries_are_not_silent_source_fallback(self):
        for reference in ("target:cpython-cp39-wrong-export", "target:cpython-build-cp38-export"):
            self.graph["target"]["second"]["contexts"]["native"] = reference
            with mock.patch.object(resolution, "python") as resolve, self.assertRaises((IdentityError, ValueError)):
                self.bind()
            resolve.assert_not_called()
        self.graph["target"]["second"]["contexts"]["native"] = "target:" + self.specs["build"]["target"]
        self.graph["target"][self.specs["build"]["target"]]["target"] = "wrong-stage"
        with mock.patch.object(resolution, "python") as resolve, self.assertRaises(IdentityError):
            self.bind()
        resolve.assert_not_called()

    def test_resolution_failure_preserves_small_evidence_and_no_successful_binding(self):
        def failure(*args):
            component_build.write_json(args[8] / "inputs.json", {"fixture": "failed"})
            raise IdentityError("signature rejected")
        with mock.patch.object(resolution, "python", side_effect=failure), self.assertRaisesRegex(IdentityError, "signature rejected"):
            self.bind()
        self.assertEqual(load_json(self.root / "evidence/cp39-build/inputs.json"), {"fixture": "failed"})
        self.assertFalse((self.root / "evidence/binding.json").exists())

    def test_changed_consumer_context_or_overlapping_output_directories_fail(self):
        resolved = copy.deepcopy(self.graph)
        resolved["target"]["second"]["contexts"]["native"] = "unverified-context"
        with mock.patch.object(resolution, "python", side_effect=self.resolve), self.assertRaisesRegex(IdentityError, "boundary changed"):
            self.bind(resolved)
        for evidence in (self.root / "data", self.root / "data/nested", self.root):
            with mock.patch.object(resolution, "python") as resolve, self.assertRaises(IdentityError):
                self.bind(evidence=evidence)
            resolve.assert_not_called()

    def test_graph_without_python_parts_needs_no_registry_and_preserves_graph(self):
        self.graph = {"target": {"consumer": {}}}
        with mock.patch.object(resolution, "python") as resolve:
            graph, result = self.bind()
        resolve.assert_not_called()
        self.assertEqual(graph, self.graph)
        self.assertEqual(result["components"], {})
        self.assertEqual(resolution.python_requirements(ROOT, self.graph), {})


if __name__ == "__main__":
    unittest.main()

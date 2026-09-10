"""CI only substitutes verified boundaries and explicitly retains missing producers."""

import copy
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_build, component_resolution as resolution
    from crossforge_internal.identity import IdentityError, load_json
    BUILD = runpy.run_path(str(ROOT / "scripts/ci-build.py"))
finally:
    sys.path.pop(0)


class ComponentBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.graph = {"group": {"default": {"targets": ["consumer"]}}, "target": {
            "consumer": {"contexts": {"x86": "target:toolchain-x86_64-build-export",
                "arm": "target:toolchain-aarch64-build-export", "runtime": "target:runtime"}},
            "gcc": {"contexts": {"tests": "target:gcc-x86_64-test-context-export",
                "install": "target:toolchain-x86_64-build-export"}}, "runtime": {}}}
        for arch, role in (("x86_64", "toolchain-install"), ("aarch64", "toolchain-install"), ("x86_64", "gcc-test-context")):
            target = component_build.toolchain_spec(arch, role)["target"]
            self.graph["target"][target] = {"target": target}

    def resolve(self, source, graph, arch, role, execution, cosign, directory, *args):
        directory.mkdir(parents=True)
        component_build.write_json(directory / "inputs.json", {"fixture": arch})
        (directory / "oci").mkdir()
        (directory / "oci/large-blob").write_text("never upload")
        if arch == "aarch64":
            result = {"status": "build-required", "reason": "catalog-index-absent"}
        else:
            result = {"status": "verified-build-component", "context": "oci-layout://verified-" + role + "@sha256:" + "a" * 64}
        component_build.write_json(directory / "resolution.json", result)
        return result

    def bind(self, evidence=None):
        return resolution.bind_toolchains(ROOT, self.graph, {}, Path("cosign"), self.root / "data",
            evidence or self.root / "evidence", "builder", Path("oras"))

    def test_each_role_resolves_once_and_every_matching_edge_is_substituted(self):
        original = copy.deepcopy(self.graph)
        with mock.patch.object(resolution, "toolchain", side_effect=self.resolve) as resolve:
            graph, result = self.bind()
        self.assertEqual(self.graph, original)
        self.assertEqual(resolve.call_count, 3)
        contexts = graph["target"]["consumer"]["contexts"]
        self.assertEqual(contexts["x86"], graph["target"]["gcc"]["contexts"]["install"])
        self.assertTrue(contexts["x86"].startswith("oci-layout://verified-toolchain-install@sha256:"))
        self.assertTrue(graph["target"]["gcc"]["contexts"]["tests"].startswith("oci-layout://verified-gcc-test-context@sha256:"))
        self.assertEqual(contexts["arm"], "target:toolchain-aarch64-build-export")
        self.assertEqual(contexts["runtime"], "target:runtime")
        self.assertEqual(result["required_producers"], ["toolchain-aarch64-build-export"])
        self.assertFalse(any(path.name in ("oci", "large-blob") for path in (self.root / "evidence").rglob("*")))

    def test_invalid_boundaries_or_unsafe_output_locations_fail_before_resolution(self):
        for evidence in (self.root / "data", self.root / "data/evidence", self.root):
            with mock.patch.object(resolution, "toolchain") as resolve, self.assertRaises(IdentityError):
                self.bind(evidence)
            resolve.assert_not_called()
        self.graph["target"]["toolchain-x86_64-build-export"]["target"] = "wrong-stage"
        with mock.patch.object(resolution, "toolchain") as resolve, self.assertRaises(IdentityError):
            self.bind()
        resolve.assert_not_called()

    def test_no_component_edges_needs_no_registry_and_does_not_change_the_graph(self):
        self.graph = {"target": {"check": {}}, "group": {"default": {"targets": ["check"]}}}
        with mock.patch.object(resolution, "toolchain") as resolve:
            graph, result = self.bind()
        self.assertEqual(graph, self.graph)
        self.assertEqual(result["components"], {})
        self.assertEqual(result["required_producers"], [])
        resolve.assert_not_called()

    def test_verifier_error_never_becomes_source_fallback_and_keeps_diagnostics(self):
        def fail(*args):
            directory = args[6]
            component_build.write_json(directory / "inputs.json", {"fixture": "failed authentication"})
            raise IdentityError("invalid signature")
        with mock.patch.object(resolution, "toolchain", side_effect=fail), self.assertRaisesRegex(IdentityError, "invalid signature"):
            self.bind()
        self.assertFalse((self.root / "evidence/binding.json").exists())
        self.assertEqual(len(list((self.root / "evidence").glob("*/inputs.json"))), 1)

    def test_hosted_stage_uses_bound_graph_and_original_roots_with_existing_timeout(self):
        function = BUILD["run_stage"]
        commands = []
        graph = {"group": {"default": {"targets": ["sdk"]}}, "target": {"sdk": {"output": [{"type": "cacheonly"}]}}}
        options = {"builder": "verified-builder", "oras": Path("oras"), "cosign": Path("cosign"), "directory": self.root / "data"}
        def execute(command, *args, **kwargs):
            commands.append(command)
            return 0
        patches = {"selected_graph": lambda *args: graph, "cache_catalog": lambda: ["sdk"],
            "monitor_resources": lambda path, stop: None, "sample_resources": lambda path: None,
            "HEARTBEAT": {"execute": execute}}
        with mock.patch.dict(function.__globals__, patches), \
             mock.patch.object(component_build, "execution_identity", return_value={}), \
             mock.patch.object(resolution, "bind_toolchains", return_value=(graph, {"components": {}, "required_producers": []})) as bind:
            status = function("sdk", self.root / "diagnostics", "ghcr.io/test/cache", components=options)
        self.assertEqual(status, 0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0:3], ["timeout", "--signal=TERM", "--kill-after=60s"])
        self.assertIn("verified-builder", commands[0])
        self.assertIn(str(self.root / "diagnostics/components.bake.json"), commands[0])
        self.assertEqual(bind.call_args[0][5], self.root / "diagnostics/components")
        self.assertEqual(load_json(self.root / "diagnostics/result.json")["exit_code"], 0)

    def test_cli_requires_complete_component_options_and_keeps_cold_and_cache_writers_separate(self):
        main = BUILD["main"]
        common = ["ci-build.py", "run", "sdk", "--directory", str(self.root / "diagnostics")]
        options = ["--component-builder", "builder", "--component-oras", "oras",
                   "--component-cosign", "cosign", "--component-directory", str(self.root / "data")]
        stage = mock.Mock(return_value=0)
        with mock.patch.dict(main.__globals__, run_stage=stage), mock.patch.object(sys, "argv", common + options):
            self.assertEqual(main(), 0)
        self.assertEqual(stage.call_args[0][-1]["builder"], "builder")
        stage.reset_mock()
        for arguments in (common + options[:2], [common[0], "--cold"] + common[1:] + options,
                          [common[0], "--write-cache"] + common[1:] + options):
            with mock.patch.dict(main.__globals__, run_stage=stage, require_writer=lambda env: None), \
                 mock.patch.object(sys, "argv", arguments), self.assertRaises(ValueError):
                main()
        stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()

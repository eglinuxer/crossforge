"""The local handoff must keep roles, source inputs, and destinations explicit."""

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import component_build as build
    from crossforge_internal.identity import IdentityError, load_json
finally:
    sys.path.pop(0)


class ComponentBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "docker").mkdir()
        (self.root / "scripts").mkdir()
        shutil.copytree(ROOT / "scripts/crossforge_internal", self.root / "scripts/crossforge_internal")
        shutil.copyfile(ROOT / "scripts/component-artifact.py", self.root / "scripts/component-artifact.py")
        (self.root / ".dockerignore").write_text(".git/\n")
        (self.root / "source").write_text("source fixture")
        (self.root / "docker/Dockerfile").write_text("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 +
            "\nFROM scratch AS gcc-x86_64\nCOPY source /work/build/gcc-x86_64/input\n"
            "FROM gcc-x86_64 AS gcc-x86_64-test-context-export\n"
            "FROM scratch AS toolchain-x86_64-build-export\nCOPY --from=gcc-x86_64 / /opt/crossforge/\n")
        self.graph = {"target": {"toolchain-x86_64-build-export": {"context": ".", "dockerfile": "docker/Dockerfile",
            "target": "toolchain-x86_64-build-export", "platforms": ["linux/amd64"],
            "cache-to": [{"type": "registry", "ref": "registry/forbidden"}],
            "tags": ["registry/forbidden:latest"], "output": [{"type": "registry"}]}}}
        self.execution = {"buildkit_image": "moby/buildkit:test@sha256:" + "b" * 64}
        self.producer = {"kind": "local", "source_commit": "a" * 40, "source_dirty": True,
                         "invocation": "urn:crossforge:local:fixture", "started_at": "2026-09-10T00:00:00Z"}

    def plan(self, directory, role="toolchain-install", graph=None):
        return build.plan_toolchain(self.root, graph or self.graph, "x86_64", role,
                                    self.execution, self.producer, directory)

    def test_local_producer_cannot_inherit_registry_publication(self):
        directory = self.root / "install"
        contract = self.plan(directory)
        graph = load_json(directory / "producer.bake.json")
        for target in graph["target"].values():
            self.assertNotIn("cache-to", target)
            self.assertNotIn("tags", target)
            self.assertTrue(all(record["type"] in ("cacheonly", "oci") for record in target["output"]))
        artifact = graph["target"]["component-artifact"]
        self.assertEqual(artifact["contexts"]["payload"], "target:toolchain-x86_64-build-export")
        self.assertIn("/component/contract.json", artifact["dockerfile-inline"])
        self.assertEqual(load_json(directory / "metadata/component/contract.json"), contract)
        self.assertEqual(load_json(directory / "inputs.json"), contract["inputs"])
        self.assertEqual(self.graph["target"]["toolchain-x86_64-build-export"]["output"], [{"type": "registry"}])

    def test_existing_output_cannot_replace_a_previous_producer(self):
        directory = self.root / "install"
        self.plan(directory)
        with self.assertRaisesRegex(IdentityError, "must be new"):
            self.plan(directory)

    def test_test_context_is_distinct_from_install_artifact(self):
        graph = {"target": {"gcc-x86_64-test-context-export": dict(
            self.graph["target"]["toolchain-x86_64-build-export"], target="gcc-x86_64-test-context-export")}}
        directory = self.root / "test-context"
        contract = self.plan(directory, "gcc-test-context", graph)
        self.assertEqual(contract["inputs"]["component"], "toolchain/x86_64-test-context")
        lines = load_json(directory / "producer.bake.json")["target"]["component-artifact"]["dockerfile-inline"]
        self.assertIn("/work/prepared/gcc/", lines)
        self.assertIn("/work/build/gcc-x86_64/", lines)
        self.assertNotIn("/opt/crossforge/", lines)

    def test_canonical_build_stage_cannot_be_swapped_for_dev_or_skeleton(self):
        for stage in ("toolchain-x86_64-dev", "sdk-skeleton", "gcc-x86_64"):
            graph = copy.deepcopy(self.graph)
            graph["target"]["toolchain-x86_64-build-export"]["target"] = stage
            with self.assertRaisesRegex(IdentityError, "canonical Docker stage"):
                self.plan(self.root / "output", graph=graph)

    def test_actual_builder_identity_mismatch_fails_before_creating_output(self):
        directory = self.root / "output"
        with mock.patch.object(build, "execution_identity", return_value={"buildkit_image": "different"}):
            with self.assertRaisesRegex(IdentityError, "execution environment differs"):
                build.produce_toolchain(self.root, self.graph, "x86_64", "toolchain-install",
                    self.execution, self.producer, directory, "fixture")
        self.assertFalse(directory.exists())

    def test_control_plane_edit_preserves_compiler_identity_but_payload_copy_change_does_not(self):
        first = build.toolchain_inputs(self.root, self.graph, "x86_64", "toolchain-install", self.execution)
        (self.root / "scripts/component-artifact.py").write_text("# a transport CLI change\n")
        second = build.toolchain_inputs(self.root, self.graph, "x86_64", "toolchain-install", self.execution)
        self.assertEqual(first, second)
        self.assertEqual(first["parameters"]["material_model"], 2)
        with mock.patch.object(build, "toolchain_spec", wraps=build.toolchain_spec) as spec:
            changed = build.toolchain_spec("x86_64", "toolchain-install")
            changed["copies"] = ["/different/layout/"]
            spec.return_value = changed
            self.assertNotEqual(first, build.toolchain_inputs(
                self.root, self.graph, "x86_64", "toolchain-install", self.execution))

    def test_buildx_metadata_uses_exact_component_target_and_digest(self):
        path = self.root / "metadata.json"
        for value in ({}, {"other": {"containerimage.digest": "sha256:" + "a" * 64}},
                      {"component-artifact": {}}, {"component-artifact": {"containerimage.digest": "latest"}}):
            path.write_text(json.dumps(value))
            with self.assertRaises(IdentityError):
                build._build_digest(path)
        path.write_text(json.dumps({"component-artifact": {"containerimage.digest": "sha256:" + "a" * 64}}))
        self.assertEqual(build._build_digest(path), "sha256:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()

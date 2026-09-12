"""Candidate assembly keeps qualification while ending at raw component bytes."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_candidate_publication as publication_fixtures
import test_component_recovery as recovery_fixtures

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import candidate_components as candidate, component_build, component_ci, component_inputs
    from crossforge_internal import component_recovery, component_resolution, python_components
    from crossforge_internal.identity import IdentityError, content_sha256, load_json
finally:
    sys.path.pop(0)


class CandidateComponentGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = json.loads(subprocess.check_output(["docker", "buildx", "bake", "--print", "sdk-candidate"], cwd=str(ROOT)))

    def setUp(self):
        self.fixture = publication_fixtures.PublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.binding = self.fixture.source / "source-binding.json"
        self.commit = self.fixture.original["source_commit"]
        self.execution = {"buildkit_image": "moby/buildkit:fixture@sha256:" + "a" * 64}
        self.directory = self.root / "prepared"
        self.producer = dict(recovery_fixtures.selection()["producer"], source_commit=self.commit)
        self.resolved = []

    def result(self, settings, directory):
        self.resolved.append(settings["component"])
        result = recovery_fixtures.selection(settings["component"], settings["role"])
        digest = "sha256:" + hashlib.sha256(settings["component"].encode()).hexdigest()
        receipt = {"artifact": {"platform_digest": digest}}
        component_build.write_json(directory / "receipt.json", receipt)
        result.update(context="oci-layout://" + str(directory / "oci") + "@" + digest,
                      reference=component_recovery.REPOSITORY + "@" + digest,
                      subject={"receipt": str(directory / "receipt.json"), "receipt_sha256": content_sha256(receipt),
                               "layout": str(directory / "oci")})
        return result

    def toolchain(self, source, graph, arch, role, execution, cosign, directory, *args):
        return self.result(dict(component_build.toolchain_spec(arch, role), role=role), directory)

    def python(self, source, graph, row, arch, kind, execution, subjects, cosign, directory, *args):
        return self.result(python_components.spec(source, row, arch, kind), directory)

    def source_graph(self, source, targets, directory, *args):
        self.assertEqual(targets, ["sdk-candidate"])
        directory.mkdir(parents=True)
        return copy.deepcopy(self.graph)

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(component_ci, "checked_source", return_value=self.producer))
        stack.enter_context(mock.patch.object(component_ci, "source_graph", side_effect=self.source_graph))
        stack.enter_context(mock.patch.object(component_build, "execution_identity", return_value=self.execution))
        stack.enter_context(mock.patch.object(component_resolution, "toolchain", side_effect=self.toolchain))
        stack.enter_context(mock.patch.object(component_resolution, "python", side_effect=self.python))
        return stack

    def prepare(self):
        return candidate.prepare(ROOT, self.binding, self.directory, self.root / "data", "fixture", Path("oras"), Path("cosign"))

    def test_real_candidate_graph_keeps_all_gates_without_compiler_source_inputs(self):
        with self.patches():
            ready = self.prepare()
            self.assertEqual(candidate.check(ROOT, self.binding, self.directory, content_sha256(ready), "fixture"), ready)
        self.assertEqual(len(ready["selection"]["components"]), 33)
        self.assertEqual(len(self.resolved), len(set(self.resolved)))
        inputs = ready["inputs"]
        paths = {item["path"] for item in inputs["files"]}
        self.assertFalse(paths & candidate.COMPILERS)
        self.assertTrue({"scripts/qualify-complete-sdk.py", "scripts/qualify-final-sdk.py", "scripts/verify-gcc-testsuite-report.py"} <= paths)
        graph = load_json(self.directory / "components.bake.json")
        self.assertEqual(graph["target"]["sdk-candidate"]["args"]["CROSSFORGE_SOURCE_BUNDLE_DIGEST"], self.fixture.binding["digest"])
        self.assertEqual(graph["group"]["default"]["targets"], ["sdk-candidate"])
        self.assertTrue(all(value.get("type") == "cacheonly" for target in graph["target"].values() for value in target.get("output", [])))
        self.assertTrue(all("/runs/123/attempts/2" in value["producer"]["invocation"] for value in ready["selection"]["components"].values()))

    def test_missing_components_or_authentication_failure_never_emit_a_ready_publication_graph(self):
        for mode in ("missing", "signature"):
            with self.patches(), tempfile.TemporaryDirectory() as directory:
                self.directory = Path(directory) / "prepared"
                if mode == "missing":
                    patch = mock.patch.object(component_resolution, "toolchain", return_value={"status": "build-required"})
                else:
                    patch = mock.patch.object(component_resolution, "toolchain", side_effect=IdentityError("bad signature"))
                with patch, self.assertRaises(IdentityError):
                    candidate.prepare(ROOT, self.binding, self.directory, Path(directory) / "data", "fixture", Path("oras"), Path("cosign"))
                self.assertFalse((self.directory / "ready.json").exists())
                self.assertFalse((self.directory / "components.bake.json").exists())

    def test_recheck_rejects_changed_binding_graph_selection_inputs_or_execution(self):
        with self.patches():
            ready = self.prepare()
            originals = {name: (self.directory / name).read_bytes() for name in ("ready.json", "components.bake.json", "component-selection.json")}
            for mode in ("digest", "graph", "selection", "inputs", "execution", "source"):
                for name, data in originals.items():
                    (self.directory / name).write_bytes(data)
                expected = content_sha256(ready)
                patch = mock.patch.object(candidate, "TARGET", candidate.TARGET)
                if mode == "digest":
                    expected = "a" * 64
                elif mode == "graph":
                    value = load_json(self.directory / "components.bake.json")
                    value["target"]["sdk-candidate"]["target"] = "sdk-complete-dev"
                    (self.directory / "components.bake.json").write_text(json.dumps(value))
                elif mode == "selection":
                    value = load_json(self.directory / "component-selection.json")
                    value["components"][next(iter(value["components"]))]["reference"] = component_recovery.REPOSITORY + "@sha256:" + "f" * 64
                    (self.directory / "component-selection.json").write_text(json.dumps(value))
                elif mode == "inputs":
                    value = copy.deepcopy(ready)
                    value["inputs"]["files"][0]["sha256"] = "f" * 64
                    (self.directory / "ready.json").write_text(json.dumps(value))
                    expected = content_sha256(value)
                elif mode == "execution":
                    patch = mock.patch.object(component_build, "execution_identity", return_value={"buildkit_image": "changed"})
                else:
                    patch = mock.patch.object(component_ci, "checked_source", return_value=dict(self.producer, source_commit="f" * 40))
                with self.subTest(mode=mode), patch, self.assertRaises(ValueError):
                    candidate.check(ROOT, self.binding, self.directory, expected, "fixture")

    def test_unbound_or_mutable_export_graph_is_rejected(self):
        with self.patches():
            graph = candidate.publication_graph(ROOT, self.fixture.binding, self.commit, self.root / "raw", "fixture")
            with self.assertRaisesRegex(IdentityError, "source compilation"):
                candidate.capture(ROOT, graph, self.execution, {})
            for field, value in (("target", "sdk-complete-dev"), ("tags", ["public:tag"]),
                                 ("output", [{"type": "image", "push": True}]), ("cache-to", [{"type": "registry"}])):
                changed = copy.deepcopy(self.graph)
                changed["target"]["sdk-candidate"][field] = value
                with mock.patch.object(component_ci, "source_graph", return_value=changed), self.subTest(field=field), self.assertRaises(IdentityError):
                    candidate.publication_graph(ROOT, self.fixture.binding, self.commit, self.root / "unused", "fixture")


class CandidateComponentWorkflowTests(unittest.TestCase):
    def test_candidate_cannot_narrow_full_selection_or_publish_without_checked_component_graph(self):
        workflow = (ROOT / ".github/workflows/candidate.yml").read_text()
        qualify = workflow.split("  qualify:\n", 1)[1].split("  source-publication:\n", 1)[0]
        self.assertIn("uses: ./.github/workflows/verify-main-incremental.yml", qualify)
        self.assertIn("profile: full", qualify)
        self.assertNotIn("selection:", qualify)
        sdk = workflow.split("  sdk-publication:\n", 1)[1].split("  publish:\n", 1)[0]
        self.assertLess(sdk.index("candidate-components.py prepare"), sdk.index("sdk-candidate.output=type=image,push=true"))
        self.assertIn('-f "$RUNNER_TEMP/candidate-components/components.bake.json" sdk-candidate', sdk)
        self.assertLess(sdk.index("sdk-candidate.output=type=image,push=true"), sdk.index("candidate-components.py check"))
        self.assertLess(sdk.index("candidate-components.py check"), sdk.index("candidate-publication.py seal --phase sdk"))
        self.assertNotIn("id-token: write", sdk)


if __name__ == "__main__":
    unittest.main()

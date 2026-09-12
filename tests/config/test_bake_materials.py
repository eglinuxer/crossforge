"""Input closures must include dependencies and fail on unsupported sources."""

import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import bake_materials as materials
    from crossforge_internal.component_inputs import identity
    from crossforge_internal.identity import IdentityError
finally:
    sys.path.pop(0)


class BakeMaterialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "docker").mkdir()
        (self.root / "scripts").mkdir()
        shutil.copytree(ROOT / "scripts/crossforge_internal", self.root / "scripts/crossforge_internal")
        for path in (".dockerignore", "scripts/build.sh", "scripts/test.sh", "selected.txt", "unrelated.txt"):
            (self.root / path).write_text("fixture\n")
        self.dockerfile = self.root / "docker/Dockerfile"
        self.dockerfile.write_text("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 + "\n"
            "FROM rocky AS common\nCOPY scripts/build.sh /build.sh\n"
            "FROM common AS build\nARG ROW=selected\nCOPY ${ROW}.txt /input\nRUN /build.sh\n"
            "FROM common AS qualification\nCOPY scripts/test.sh /test.sh\nRUN /test.sh\n"
            "FROM scratch AS output\nCOPY --from=build /out/ /out/\n")
        self.graph = {"target": {"component": {"context": ".", "dockerfile": "docker/Dockerfile",
            "target": "output", "platforms": ["linux/amd64"], "args": {"ROW": "selected"},
            "contexts": {"rocky": "docker-image://rocky:8@sha256:" + "b" * 64},
            "output": [{"type": "cacheonly"}]}}}

    def capture(self, graph=None):
        return materials.capture(self.root, graph or self.graph, "component", "toolchain/x86_64",
                                 "toolchain-install", ["x86_64-unknown-linux-gnu"], {"builder": "fixture"})

    def test_transitive_source_recipe_and_pinned_context_are_bound(self):
        value = self.capture()
        files = {record["path"] for record in value["files"]}
        self.assertIn("scripts/build.sh", files)
        self.assertIn("selected.txt", files)
        self.assertNotIn("scripts/test.sh", files)
        self.assertNotIn("unrelated.txt", files)
        recipe = value["parameters"]["recipes"]["component"]
        self.assertEqual(set(recipe["stages"]), {"common", "build", "output"})
        self.assertEqual(value["parameters"]["bake_targets"]["component"]["contexts"],
                         self.graph["target"]["component"]["contexts"])

    def test_selected_files_and_instructions_invalidate_but_unrelated_stage_does_not(self):
        baseline = identity(self.capture())
        (self.root / "scripts/test.sh").write_text("changed unrelated test")
        self.dockerfile.write_text(self.dockerfile.read_text().replace("RUN /test.sh", "RUN /test.sh --more"))
        self.assertEqual(identity(self.capture()), baseline)
        self.dockerfile.write_text(self.dockerfile.read_text().replace("RUN /build.sh", "RUN /build.sh --more"))
        self.assertNotEqual(identity(self.capture()), baseline)
        baseline = identity(self.capture())
        (self.root / "selected.txt").write_text("new input")
        self.assertNotEqual(identity(self.capture()), baseline)

    def test_copy_directory_binds_added_files_empty_directories_and_modes(self):
        directory = self.root / "tree"
        directory.mkdir()
        self.dockerfile.write_text(self.dockerfile.read_text().replace("COPY ${ROW}.txt /input", "COPY tree/ /input/"))
        baseline = identity(self.capture())
        (directory / "empty").mkdir()
        self.assertNotEqual(identity(self.capture()), baseline)
        baseline = identity(self.capture())
        (directory / "new-file").write_text("new")
        self.assertNotEqual(identity(self.capture()), baseline)
        baseline = identity(self.capture())
        (directory / "empty").chmod(0o700)
        self.assertNotEqual(identity(self.capture()), baseline)

    def test_arguments_and_images_are_bound_but_cache_and_output_locations_are_not(self):
        baseline = identity(self.capture())
        graph = copy.deepcopy(self.graph)
        graph["target"]["component"]["cache-from"] = [{"type": "registry", "ref": "cache:latest"}]
        graph["target"]["component"]["output"] = [{"type": "oci", "dest": "/different/output"}]
        self.assertEqual(identity(self.capture(graph)), baseline)
        graph["target"]["component"]["contexts"]["rocky"] = "docker-image://rocky:8@sha256:" + "c" * 64
        self.assertNotEqual(identity(self.capture(graph)), baseline)
        graph = copy.deepcopy(self.graph)
        graph["target"]["component"]["args"]["ROW"] = "unrelated"
        self.assertNotEqual(identity(self.capture(graph)), baseline)

    def test_unrelated_args_and_global_platform_declarations_do_not_rebuild(self):
        baseline = identity(self.capture())
        graph = copy.deepcopy(self.graph)
        graph["target"]["component"]["args"].update({"CROSSFORGE_SOURCE_COMMIT": "b" * 40,
                                                    "QEMU_EXECUTOR_CPU": "unused"})
        self.assertEqual(identity(self.capture(graph)), baseline)
        self.dockerfile.write_text(self.dockerfile.read_text().replace(
            "FROM rocky AS common", "ARG OTHER_PLATFORM=linux/arm64\nFROM rocky AS common"))
        self.assertEqual(identity(self.capture(graph)), baseline)
        self.dockerfile.write_text(self.dockerfile.read_text().replace(
            "FROM rocky AS common", "FROM --platform=${OTHER_PLATFORM} rocky AS common"))
        self.assertNotEqual(identity(self.capture(graph)), baseline)

    def test_transport_implementation_is_bound_only_if_recipe_copies_it(self):
        path = self.root / "scripts/crossforge_internal/identity.py"
        baseline = identity(self.capture())
        path.write_text(path.read_text() + "\n# transport-only edit\n")
        self.assertEqual(identity(self.capture()), baseline)
        self.dockerfile.write_text(self.dockerfile.read_text().replace(
            "RUN /build.sh", "COPY scripts/crossforge_internal/identity.py /identity.py\nRUN /build.sh"))
        baseline = identity(self.capture())
        path.write_text(path.read_text() + "\n# now a copied build input\n")
        self.assertNotEqual(identity(self.capture()), baseline)

    def test_redeclared_global_defaults_and_implicit_frontend_args_are_bound(self):
        self.dockerfile.write_text(self.dockerfile.read_text().replace(
            "FROM rocky AS common", "ARG FLAVOR=one\nFROM rocky AS common\nARG FLAVOR"))
        baseline = identity(self.capture())
        self.dockerfile.write_text(self.dockerfile.read_text().replace("FLAVOR=one", "FLAVOR=two"))
        self.assertNotEqual(identity(self.capture()), baseline)
        baseline = identity(self.capture())
        for name in ("SOURCE_DATE_EPOCH", "BUILDKIT_SANDBOX_HOSTNAME", "HTTPS_PROXY"):
            graph = copy.deepcopy(self.graph)
            graph["target"]["component"]["args"][name] = "changed"
            self.assertNotEqual(identity(self.capture(graph)), baseline)
        self.graph["target"]["component"]["args"]["BUILDKIT_SYNTAX"] = "untrusted"
        with self.assertRaisesRegex(IdentityError, "frontend overrides"):
            self.capture()

    def test_unknown_remote_sources_mounts_and_syntax_fail_closed(self):
        original = self.dockerfile.read_text()
        for changed in (original.replace("RUN /build.sh", "ADD https://example.com/file /file"),
                        original.replace("COPY ${ROW}.txt /input", 'COPY ["selected.txt", "/input"]'),
                        original.replace("RUN /build.sh", "RUN --mount=type=secret,id=source /build.sh"),
                        original.replace("RUN /build.sh", "RUN --mount=type=cache,target=/cache /build.sh"),
                        original.replace("ARG ROW=selected", "ENV ROW=selected"),
                        original.replace("FROM rocky AS common", "FROM unpinned:latest AS common")):
            with self.subTest(recipe=changed):
                self.dockerfile.write_text(changed)
                with self.assertRaises(IdentityError):
                    self.capture()
        self.dockerfile.write_text(original)
        for key, value in (("secret", [{"id": "source"}]), ("network", "host"),
                           ("context", "https://example.com/source.git"), ("platforms", ["linux/arm64"])):
            graph = copy.deepcopy(self.graph)
            graph["target"]["component"][key] = value
            with self.assertRaises(IdentityError):
                self.capture(graph)

    def test_linked_target_contexts_are_traversed_and_cycles_rejected(self):
        graph = copy.deepcopy(self.graph)
        graph["target"]["component"]["contexts"]["rocky"] = "target:base"
        (self.root / "docker/base.Dockerfile").write_text("# syntax=docker/dockerfile:1@sha256:" + "a" * 64 +
            "\nFROM scratch AS root\nCOPY unrelated.txt /base-input\n")
        graph["target"]["base"] = {"context": ".", "dockerfile": "docker/base.Dockerfile",
                                    "target": "root", "platforms": ["linux/amd64"]}
        files = {record["path"] for record in self.capture(graph)["files"]}
        self.assertIn("unrelated.txt", files)
        graph["target"]["base"] = copy.deepcopy(graph["target"]["component"])
        with self.assertRaisesRegex(IdentityError, "cyclic"):
            self.capture(graph)

    def test_source_deletion_symlink_and_unpinned_base_fail(self):
        (self.root / "selected.txt").unlink()
        with self.assertRaises(IdentityError):
            self.capture()
        (self.root / "selected.txt").symlink_to(self.root / "unrelated.txt")
        with self.assertRaises(IdentityError):
            self.capture()
        (self.root / "selected.txt").unlink()
        (self.root / "selected.txt").write_text("restored")
        self.graph["target"]["component"]["contexts"]["rocky"] = "docker-image://rocky:8"
        with self.assertRaises(IdentityError):
            self.capture()

    def test_target_scoped_contexts_cannot_confuse_two_architecture_subjects(self):
        self.dockerfile.write_text(self.dockerfile.read_text() +
            "FROM scratch AS merged\nCOPY --from=left /out/ /left/\nCOPY --from=right /out/ /right/\n")
        graph = copy.deepcopy(self.graph)
        graph["target"]["left"] = copy.deepcopy(self.graph["target"]["component"])
        graph["target"]["right"] = copy.deepcopy(self.graph["target"]["component"])
        graph["target"]["component"]["target"] = "merged"
        graph["target"]["component"]["contexts"] = {"left": "target:left", "right": "target:right"}
        bindings = {}
        for name, byte in (("left", "c"), ("right", "d")):
            digest = "sha256:" + byte * 64
            graph["target"][name]["contexts"]["rocky"] = "oci-layout:///" + name + "@" + digest
            bindings[name + ":rocky"] = {"component": "host/" + name, "inputs_sha256": "e" * 64, "artifact_digest": digest}
        def capture():
            return materials.capture(self.root, graph, "component", "qualification/row", "qualification",
                                     ["x86_64-unknown-linux-gnu"], {"builder": "fixture"}, artifacts=bindings)
        self.assertEqual(len(capture()["dependencies"]), 2)
        bindings["rocky"] = bindings["left:rocky"]
        with self.assertRaisesRegex(IdentityError, "ambiguous"):
            capture()
        bindings.pop("rocky")
        graph["target"]["right"]["contexts"]["rocky"] = graph["target"]["left"]["contexts"]["rocky"]
        with self.assertRaisesRegex(IdentityError, "verified artifact digest"):
            capture()

    def test_verified_component_cuts_source_closure_and_binds_inputs_and_actual_digest(self):
        record = {"component": "host/common", "inputs_sha256": "d" * 64,
                  "artifact_digest": "sha256:" + "e" * 64}
        graph = copy.deepcopy(self.graph)
        graph["target"]["component"]["contexts"]["rocky"] = "oci-layout:///first@" + record["artifact_digest"]
        def capture(bindings=None):
            return materials.capture(self.root, graph, "component", "toolchain/x86_64", "toolchain-install",
                ["x86_64-unknown-linux-gnu"], {"builder": "fixture"}, artifacts=bindings or {"rocky": record})
        value = capture()
        self.assertEqual(value["dependencies"], [record])
        graph["target"]["component"]["contexts"]["rocky"] = "oci-layout:///other-location@" + record["artifact_digest"]
        self.assertEqual(value, capture())
        changed = dict(record, inputs_sha256="f" * 64)
        self.assertNotEqual(value, capture({"rocky": changed}))
        graph["target"]["component"]["contexts"]["rocky"] = "docker-image://image@sha256:" + "f" * 64
        with self.assertRaisesRegex(IdentityError, "verified artifact digest"):
            capture()
        graph["target"]["component"]["contexts"]["rocky"] = "target:source-fallback"
        with self.assertRaisesRegex(IdentityError, "verified artifact digest"):
            capture()

    def test_unused_verified_component_is_not_accepted_as_a_tested_dependency(self):
        with self.assertRaisesRegex(IdentityError, "not consumed"):
            materials.capture(self.root, self.graph, "component", "toolchain/x86_64", "toolchain-install",
                ["x86_64-unknown-linux-gnu"], {"builder": "fixture"}, artifacts={"unused": {
                    "component": "host/common", "inputs_sha256": "d" * 64,
                    "artifact_digest": "sha256:" + "e" * 64}})


if __name__ == "__main__":
    unittest.main()

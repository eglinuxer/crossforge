"""Exercise byte verification independently from BuildKit layer application."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from crossforge_internal import oci_layout as oci
    from crossforge_internal.identity import IdentityError, canonical_bytes
finally:
    sys.path.pop(0)


class OCILayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "blobs/sha256").mkdir(parents=True)
        (self.root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        self.layer = self.blob(b"opaque compressed layer bytes", next(iter(oci.LAYERS)))
        self.config = {"os": "linux", "architecture": "amd64", "rootfs": {
            "type": "layers", "diff_ids": ["sha256:" + "1" * 64]}}
        self.manifest = {"schemaVersion": 2, "mediaType": oci.MANIFEST,
                         "config": self.blob(self.config, oci.CONFIG),
                         "layers": [self.layer]}
        self.root_descriptor = self.blob(self.manifest, oci.MANIFEST)
        self.write_index([self.root_descriptor])

    def blob(self, value, media_type):
        data = value if type(value) is bytes else canonical_bytes(value)
        digest = hashlib.sha256(data).hexdigest()
        (self.root / "blobs/sha256" / digest).write_bytes(data)
        return {"mediaType": media_type, "digest": "sha256:" + digest, "size": len(data)}

    def write_index(self, descriptors):
        value = {"schemaVersion": 2, "mediaType": oci.INDEX, "manifests": descriptors}
        (self.root / "index.json").write_bytes(canonical_bytes(value))
        return value

    def inspect(self, root=None):
        return oci.inspect(self.root, (root or self.root_descriptor)["digest"])

    def test_manifest_digest_verifies_config_and_every_layer(self):
        result = self.inspect()
        self.assertEqual(result["platform_digest"], self.root_descriptor["digest"])
        self.assertEqual(result["config_digest"], self.manifest["config"]["digest"])
        self.assertEqual(result["layer_digests"], [self.layer["digest"]])
        self.assertEqual(result["compressed_layer_bytes"], self.layer["size"])

    def test_nested_buildx_index_selects_platform_and_ignores_attestation(self):
        platform = dict(self.root_descriptor, platform={"os": "linux", "architecture": "amd64"})
        attestation = dict(self.root_descriptor, platform={"os": "unknown", "architecture": "unknown"})
        inner = self.blob(self.write_index([platform, attestation]), oci.INDEX)
        outer = self.blob(self.write_index([inner]), oci.INDEX)
        self.assertEqual(self.inspect(outer)["platform_digest"], self.root_descriptor["digest"])

    def test_layout_index_cannot_redirect_an_independently_pinned_digest(self):
        self.write_index([])
        self.assertEqual(self.inspect()["root_digest"], self.root_descriptor["digest"])

    def test_missing_wrong_and_tampered_blobs_fail(self):
        with self.assertRaises(IdentityError):
            oci.inspect(self.root, "sha256:" + "0" * 64)
        for descriptor in (self.root_descriptor, self.manifest["config"], self.layer):
            with self.subTest(digest=descriptor["digest"]):
                path = self.root / "blobs/sha256" / descriptor["digest"].split(":")[1]
                original = path.read_bytes()
                path.write_bytes(original + b"tampered")
                with self.assertRaises(IdentityError):
                    self.inspect()
                path.unlink()
                with self.assertRaises(IdentityError):
                    self.inspect()
                path.write_bytes(original)

    def test_layer_size_and_media_type_are_verified(self):
        for key, value in (("size", self.layer["size"] + 1), ("size", True),
                           ("size", -1), ("mediaType", "application/unknown"),
                           ("digest", "sha256:" + "1" * 64 + "\n")):
            manifest = copy.deepcopy(self.manifest)
            manifest["layers"][0][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(IdentityError):
                    self.inspect(self.blob(manifest, oci.MANIFEST))

    def test_config_platform_and_rootfs_contract_are_required(self):
        for key, value in (("os", "windows"), ("architecture", "arm64"), ("variant", "v3"),
                           ("rootfs", {"type": "layers", "diff_ids": []}),
                           ("rootfs", {"type": "layers", "diff_ids": ["not-a-digest"]})):
            config = dict(self.config, **{key: value})
            manifest = dict(self.manifest, config=self.blob(config, oci.CONFIG))
            with self.subTest(key=key, value=value):
                with self.assertRaises(IdentityError):
                    self.inspect(self.blob(manifest, oci.MANIFEST))

    def test_ambiguous_and_unsupported_platform_indexes_fail(self):
        platform = dict(self.root_descriptor, platform={"os": "linux", "architecture": "amd64"})
        for descriptors in ([], [platform, platform], [self.root_descriptor],
                            [dict(platform, platform={"os": "linux", "architecture": "arm64"})],
                            [dict(platform, platform={"os": "linux", "architecture": "amd64", "variant": "v3"})]):
            with self.subTest(descriptors=descriptors):
                with self.assertRaises(IdentityError):
                    self.inspect(self.blob(self.write_index(descriptors), oci.INDEX))

    def test_index_recursion_is_bounded(self):
        root = dict(self.root_descriptor, platform={"os": "linux", "architecture": "amd64"})
        for _ in range(9):
            root = self.blob(self.write_index([root]), oci.INDEX)
        with self.assertRaisesRegex(IdentityError, "nesting"):
            self.inspect(root)

    def test_symlinks_duplicate_json_keys_and_bad_layout_headers_fail(self):
        path = self.root / "oci-layout"
        original = path.read_bytes()
        for content in (b'{"imageLayoutVersion":"2.0.0"}',
                        b'{"imageLayoutVersion":"wrong","imageLayoutVersion":"1.0.0"}'):
            path.write_bytes(content)
            with self.assertRaises(IdentityError):
                self.inspect()
        path.unlink()
        (self.root / "header").write_bytes(original)
        path.symlink_to(self.root / "header")
        with self.assertRaises(IdentityError):
            self.inspect()

    def test_cli_reports_observation_and_rejects_wrong_digest(self):
        command = [sys.executable, str(ROOT / "scripts/component-artifact.py"), "inspect-layout",
                   "--layout", str(self.root), "--digest", self.root_descriptor["digest"]]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["root_digest"], self.root_descriptor["digest"])
        command[-1] = "latest"
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)


class MetadataGraphTests(unittest.TestCase):
    def graph(self, **changes):
        values = {"reference": "oci-layout:///work/component@sha256:" + "a" * 64,
                  "copies": {"component/inputs.json": "inputs.json"},
                  "destination": "/tmp/metadata", "frontend": "docker/dockerfile:1@sha256:" + "b" * 64}
        values.update(changes)
        return oci.metadata_graph(**values)

    def test_extraction_uses_pinned_context_copy_only_and_scratch(self):
        target = self.graph()["target"]["component-metadata"]
        self.assertEqual(target["platforms"], ["linux/amd64"])
        self.assertIn('COPY --from=component ["/component/inputs.json", "/inputs.json"]', target["dockerfile-inline"])
        self.assertNotIn("RUN", target["dockerfile-inline"])
        self.assertNotIn("target:", target["contexts"]["component"])

    def test_unpinned_references_and_frontend_injection_fail(self):
        for field, values in (("reference", ["docker-image://image:latest", "target:toolchain",
                                            "oci-layout://@sha256:" + "a" * 64,
                                            "oci-layout:///tmp\nx@sha256:" + "a" * 64]),
                              ("frontend", ["docker/dockerfile:1", "docker/dockerfile:1\nRUN bad@sha256:" + "b" * 64])):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(IdentityError):
                        self.graph(**{field: value})

    def test_unsafe_or_duplicate_metadata_paths_fail(self):
        for copies in ({}, {"etc/passwd": "out"}, {"component/../bad": "out"},
                       {"component/one": "../out"}, {"component/one": "out", "component/two": "out"},
                       {1: "out", "component/one": "two"}):
            with self.subTest(copies=copies):
                with self.assertRaises(IdentityError):
                    self.graph(copies=copies)


if __name__ == "__main__":
    unittest.main()

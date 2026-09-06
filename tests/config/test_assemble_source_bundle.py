import ast
import hashlib
import json
import runpy
import tempfile
import unittest
from collections import Counter
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/assemble-source-bundle.py"
ASSEMBLER = runpy.run_path(str(SCRIPT))
IDENTITY_SCRIPT = REPOSITORY / "scripts/source-bundle-identity.py"
IDENTITY = runpy.run_path(str(IDENTITY_SCRIPT))


class AssembleSourceBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = ASSEMBLER["BUILD"]
        cls.release = build["load_schema_document"](
            REPOSITORY / "config/release.json",
            REPOSITORY / "config/schemas/release.schema.json",
        )
        cls.lock = build["load_schema_document"](
            REPOSITORY / "locks/rpm-source-el8.json",
            REPOSITORY / "config/schemas/rpm-source-lock.schema.json",
        )

    def test_complete_plan_has_exact_product_qualification_and_evidence_sets(self):
        self.assertEqual(
            self.release["source_bundle"]["archive"]["entries"], 384
        )
        self.assertEqual(
            self.release["source_bundle"]["archive"]["publication"],
            "same-public-oci-package",
        )
        entries = ASSEMBLER["expected_entries"](
            self.release, self.lock, "1" * 40
        )
        self.assertEqual(len(entries), 384)
        self.assertEqual(
            Counter(record["scope"] for record in entries.values()),
            Counter(
                {
                    "product": 348,
                    "qualification": 3,
                    "verification": 23,
                    "metadata": 10,
                }
            ),
        )
        self.assertEqual(
            Counter(record["kind"] for record in entries.values())["source-rpm"],
            333,
        )
        for path in (
            "sources/product/cmake/cmake-4.4.0.tar.gz",
            "sources/product/qemu/qemu-10.2.3.tar.xz",
            "sources/product/qemu/binfmt-e29e7d72c9672c8c8bf846655ab149b50e1a62bd.tar.gz",
            "sources/product/vcpkg/vcpkg-tool-98d7cb0cf1f4686a3e43aa5672b6230c1d56bce8.tar.gz",
            "sources/qualification/qt/qt-everywhere-opensource-src-6.8.4.tar.xz",
            "verification/cpython/Python-3.14.7.tar.xz.sigstore",
        ):
            self.assertIn(path, entries)
        with self.assertRaisesRegex(
            ASSEMBLER["ValidationError"], "unbound"
        ):
            ASSEMBLER["expected_entries"](
                self.release, self.lock, "0" * 40
            )

    def test_generic_tree_validation_rejects_tampering_and_extra_files(self):
        payload = b"source"
        expected = {
            "sources/example.tar.gz": {
                "scope": "product",
                "kind": "source-archive",
                "component": "sources/example",
                "origin": "https://example.invalid/example.tar.gz",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "sources/example.tar.gz"
            path.parent.mkdir()
            path.write_bytes(payload)
            entries = ASSEMBLER["validate_files"](root, expected)
            self.assertEqual(entries[0]["sha256"], expected[path.relative_to(root).as_posix()]["sha256"])
            path.write_bytes(payload + b"tampered")
            with self.assertRaises(ASSEMBLER["ValidationError"]):
                ASSEMBLER["validate_files"](root, expected)
            path.write_bytes(payload)
            (root / "extra").write_bytes(b"extra")
            with self.assertRaisesRegex(
                ASSEMBLER["ValidationError"], "membership"
            ):
                ASSEMBLER["validate_files"](root, expected)

    def test_source_bundle_graph_is_explicit_and_candidate_bound(self):
        dockerfile = (REPOSITORY / "docker/source-bundle.Dockerfile").read_text(
            encoding="utf-8"
        )
        bake = (REPOSITORY / "docker-bake.hcl").read_text(encoding="utf-8")
        self.assertIn('target "source-bundle"', bake)
        self.assertIn('target "source-bundle-identity"', bake)
        self.assertIn(
            'default = "0000000000000000000000000000000000000000"', bake
        )
        for context in (
            "crossforge_rpm_sources",
            "crossforge_qemu_source",
            "crossforge_cmake_source",
            "crossforge_vcpkg_source",
            "crossforge_cpython_cp314",
        ):
            self.assertIn(context, bake)
            self.assertIn("--from=" + context, dockerfile)
        self.assertIn("assemble-source-bundle.py", dockerfile)
        self.assertIn("--source-commit \"$CROSSFORGE_SOURCE_COMMIT\"", dockerfile)
        self.assertIn("RUN --network=none", dockerfile)
        self.assertNotIn("$(git ", dockerfile)
        self.assertIn("clean_before_tool_injection", SCRIPT.read_text(encoding="utf-8"))
        dockerignore = (REPOSITORY / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn(".git/", dockerignore)
        self.assertIn(".agents/", dockerignore)
        self.assertIn(".codex/", dockerignore)
        self.assertIn("**/__pycache__/", dockerignore)
        self.assertIn("**/*.py[cod]", dockerignore)
        self.assertIn(
            'sha256sum "crossforge-source-${CROSSFORGE_SOURCE_COMMIT}.tar.zst"',
            dockerfile,
        )
        self.assertIn("source-bundle-identity.py", dockerfile)

    def test_archive_identity_requires_a_portable_exact_checksum(self):
        commit = "1" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / ("crossforge-source-%s.tar.zst" % commit)
            archive.write_bytes(b"source archive")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksum = root / (archive.name + ".sha256")
            checksum.write_text(
                "%s  %s\n" % (digest, archive.name), encoding="ascii"
            )
            manifest = root / "MANIFEST.json"
            manifest.write_text(
                json.dumps(
                    {
                        "kind": "crossforge-source-bundle",
                        "source_commit": commit,
                        "release_sha256": "2" * 64,
                    }
                ),
                encoding="utf-8",
            )
            identity = IDENTITY["create_identity"](
                archive, checksum, manifest, commit
            )
            self.assertEqual(identity["archive"]["sha256"], digest)
            checksum.write_text(
                "%s  /out/%s\n" % (digest, archive.name), encoding="ascii"
            )
            with self.assertRaisesRegex(
                IDENTITY["ValidationError"], "checksum differs"
            ):
                IDENTITY["create_identity"](
                    archive, checksum, manifest, commit
                )

    def test_manifest_schema_and_assembler_are_python36_compatible(self):
        schema = ASSEMBLER["STRICT"]["load_json"](
            REPOSITORY / "config/schemas/source-bundle-manifest.schema.json"
        )
        ASSEMBLER["STRICT"]["validate_schema_subset"](schema)
        for path in (SCRIPT, IDENTITY_SCRIPT):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 6),
            )


if __name__ == "__main__":
    unittest.main()

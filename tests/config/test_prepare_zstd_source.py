import ast
import copy
import hashlib
import io
import json
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts/prepare-zstd-source.py"
PREPARER = runpy.run_path(str(SCRIPT))
RENDERER = runpy.run_path(str(REPOSITORY / "scripts/render-release-components.py"))


class PrepareZstdSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        release = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )
        components = RENDERER["render_component_documents"](release)
        self.component = components["sources/zstd"]
        self.policy = components["implementation/zstd-build-policy"]
        self.digest = RENDERER["canonical_sha256"](self.component)
        self.component_path = self.directory / "zstd.json"
        self.component_path.write_text(
            json.dumps(self.component, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.policy_digest = RENDERER["canonical_sha256"](self.policy)
        self.policy_path = self.directory / "policy.json"
        self.policy_path.write_text(
            json.dumps(self.policy, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.archive = Path("/tmp/zstd-1.5.7.tar.gz")
        self.signature = Path("/tmp/zstd-1.5.7.tar.gz.sig")

    def tearDown(self):
        self.temporary.cleanup()

    def test_tracked_component_prepares_real_signed_source_subset(self):
        if not self.archive.is_file() or not self.signature.is_file():
            self.skipTest("real zstd release assets are unavailable")
        destination = self.directory / "zstd-1.5.7"
        manifest = self.directory / "source.json"
        identity = PREPARER["prepare"](
            self.component_path,
            self.digest,
            self.archive,
            self.signature,
            destination,
            manifest,
            REPOSITORY,
        )
        self.assertEqual(identity["version"], "1.5.7")
        self.assertEqual(identity["component"]["canonical_sha256"], self.digest)
        self.assertEqual(identity["signature"]["fingerprint"], "4ef4ac63455fc9f4545d9b7def8fe99528b52ffd")
        self.assertTrue((destination / "lib/zstd.h").is_file())
        self.assertTrue((destination / "LICENSE").is_file())
        self.assertTrue((destination / "COPYING").is_file())
        self.assertFalse((destination / "programs").exists())
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), identity)

    def test_wrong_component_digest_fails_before_extraction(self):
        with self.assertRaises(PREPARER["PreparationError"]):
            PREPARER["load_identity"](
                self.component_path, "0" * 64, REPOSITORY
            )

    def test_selected_archive_path_traversal_is_rejected(self):
        archive_path = self.directory / "unsafe.tar.gz"
        with tarfile.open(str(archive_path), "w:gz") as archive:
            member = tarfile.TarInfo("zstd-1.5.7/lib/../../escape.c")
            payload = b"escape"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        with tarfile.open(str(archive_path), "r:gz") as archive:
            with self.assertRaisesRegex(
                PREPARER["PreparationError"], "unsafe zstd archive member"
            ):
                PREPARER["safe_members"](archive, "1.5.7")

    def test_preparer_is_python36_syntax_compatible(self):
        ast.parse(
            SCRIPT.read_text(encoding="utf-8"),
            filename=str(SCRIPT),
            feature_version=(3, 6),
        )

    def test_builder_contract_checks_do_not_use_optimizable_asserts(self):
        builder = (REPOSITORY / "scripts/build-zstd.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\nassert ", builder)

    def prepared_fixture(self):
        source = self.directory / "prepared/zstd"
        (source / "lib").mkdir(parents=True)
        (source / "lib/Makefile").write_bytes(b"fixture\n")
        (source / "lib/zstd.h").write_bytes(b"fixture header\n")
        (source / "LICENSE").write_bytes(b"fixture license\n")
        (source / "COPYING").write_bytes(b"fixture copying\n")

        archive = self.directory / "fixture-zstd.tar.gz"
        with tarfile.open(str(archive), "w:gz") as stream:
            stream.add(str(source), arcname="zstd-1.5.7")
        signature = self.directory / "fixture-zstd.tar.gz.sig"
        signature.write_bytes(b"fixture detached signature\n")

        component = copy.deepcopy(self.component)
        values = {item["path"]: item for item in component["materials"]}
        values["/python/zstd/source/sha256"]["value"] = hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()
        values["/python/zstd/source/size"]["value"] = archive.stat().st_size
        values["/python/zstd/source/signature/sha256"]["value"] = hashlib.sha256(
            signature.read_bytes()
        ).hexdigest()
        values["/python/zstd/source/signature/size"]["value"] = (
            signature.stat().st_size
        )
        values["/python/zstd/license/license_sha256"]["value"] = hashlib.sha256(
            (source / "LICENSE").read_bytes()
        ).hexdigest()
        values["/python/zstd/license/copying_sha256"]["value"] = hashlib.sha256(
            (source / "COPYING").read_bytes()
        ).hexdigest()
        component_path = self.directory / "fixture-component.json"
        component_path.write_text(
            json.dumps(component, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        digest = RENDERER["canonical_sha256"](component)
        identity = PREPARER["load_identity"](
            component_path, digest, REPOSITORY
        )
        document = PREPARER["source_manifest_document"](
            identity, digest, source
        )
        manifest = self.directory / "prepared/source.json"
        manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return source, component_path, digest, manifest, document, archive, signature

    def test_prepared_manifest_is_rebound_to_authenticated_component(self):
        source, component, digest, manifest, document, archive, signature = (
            self.prepared_fixture()
        )
        self.assertEqual(
            PREPARER["validate_prepared_source"](
                component, digest, source, manifest, archive, signature, REPOSITORY
            ),
            document,
        )
        with self.assertRaisesRegex(
            PREPARER["PreparationError"], "component canonical SHA256 differs"
        ):
            PREPARER["validate_prepared_source"](
                component,
                "0" * 64,
                source,
                manifest,
                archive,
                signature,
                REPOSITORY,
            )

    def test_prepared_manifest_identity_and_unknown_field_tampering_is_rejected(self):
        source, component, digest, manifest, original, archive, signature = (
            self.prepared_fixture()
        )
        mutations = {
            "component name": lambda value: value["component"].__setitem__(
                "name", "sources/not-zstd"
            ),
            "component digest": lambda value: value["component"].__setitem__(
                "canonical_sha256", "0" * 64
            ),
            "source": lambda value: value["source"].__setitem__(
                "sha256", "0" * 64
            ),
            "signature": lambda value: value["signature"].__setitem__(
                "fingerprint", "0" * 40
            ),
            "git": lambda value: value["git"].__setitem__(
                "commit", "0" * 40
            ),
            "license": lambda value: value["license"].__setitem__(
                "sha256", "0" * 64
            ),
            "top-level unknown field": lambda value: value.__setitem__(
                "unexpected", True
            ),
            "nested unknown field": lambda value: value["signature"].__setitem__(
                "unexpected", True
            ),
            "file-record unknown field": lambda value: value["files"][0].__setitem__(
                "unexpected", True
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                document = copy.deepcopy(original)
                mutate(document)
                manifest.write_text(
                    json.dumps(document, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(PREPARER["PreparationError"]):
                    PREPARER["validate_prepared_source"](
                        component,
                        digest,
                        source,
                        manifest,
                        archive,
                        signature,
                        REPOSITORY,
                    )

    def test_self_consistent_tree_inventory_cannot_replace_locked_archive(self):
        source, component, digest, manifest, document, archive, signature = (
            self.prepared_fixture()
        )
        (source / "lib/zstd.h").write_bytes(b"tampered header\n")
        with self.assertRaisesRegex(
            PREPARER["PreparationError"], "prepared zstd source manifest"
        ):
            PREPARER["validate_prepared_source"](
                component,
                digest,
                source,
                manifest,
                archive,
                signature,
                REPOSITORY,
            )

        # Even a manifest recomputed to describe the substituted tree must fail:
        # the separately supplied archive is content-locked by the component.
        document["files"] = PREPARER["tree_manifest"](source)
        manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PREPARER["PreparationError"], "versus locked archive"
        ):
            PREPARER["validate_prepared_source"](
                component,
                digest,
                source,
                manifest,
                archive,
                signature,
                REPOSITORY,
            )

    def test_exported_archive_and_signature_must_match_component_materials(self):
        source, component, digest, manifest, _, archive, signature = (
            self.prepared_fixture()
        )
        archive_bytes = archive.read_bytes()
        signature_bytes = signature.read_bytes()
        signature.write_bytes(signature_bytes + b"tampered")
        with self.assertRaisesRegex(
            PREPARER["PreparationError"], "detached signature identity differs"
        ):
            PREPARER["validate_prepared_source"](
                component,
                digest,
                source,
                manifest,
                archive,
                signature,
                REPOSITORY,
            )

        signature.write_bytes(signature_bytes)
        archive.write_bytes(archive_bytes + b"tampered")
        with self.assertRaisesRegex(
            PREPARER["PreparationError"], "source archive identity differs"
        ):
            PREPARER["validate_prepared_source"](
                component,
                digest,
                source,
                manifest,
                archive,
                signature,
                REPOSITORY,
            )

    def test_builder_rejects_a_self_asserted_source_component_digest(self):
        source, component, digest, manifest, document, archive, signature = (
            self.prepared_fixture()
        )
        document["component"]["canonical_sha256"] = "0" * 64
        manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(REPOSITORY / "scripts/build-zstd.sh"),
                str(source),
                str(self.directory / "build-rejected"),
                str(self.directory / "prefix-rejected"),
                str(manifest),
                str(archive),
                str(signature),
                str(self.directory / "build-rejected.json"),
                str(component),
                digest,
                str(self.policy_path),
                self.policy_digest,
                "zstd/host-build",
                "0" * 64,
                "host",
                "/usr/bin",
                "-",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "prepared zstd source manifest.component.canonical_sha256 differs",
            result.stdout,
        )

    def test_real_prepared_source_builds_static_host_archive(self):
        if not self.archive.is_file() or not self.signature.is_file():
            self.skipTest("real zstd release assets are unavailable")
        source = self.directory / "source/zstd-1.5.7"
        source_manifest = self.directory / "source/source.json"
        PREPARER["prepare"](
            self.component_path,
            self.digest,
            self.archive,
            self.signature,
            source,
            source_manifest,
            REPOSITORY,
        )
        build = self.directory / "build"
        prefix = self.directory / "deps/zstd/1.5.7/host"
        build_manifest = self.directory / "build.json"
        result = subprocess.run(
            [
                str(REPOSITORY / "scripts/build-zstd.sh"),
                str(source),
                str(build),
                str(prefix),
                str(source_manifest),
                str(self.archive),
                str(self.signature),
                str(build_manifest),
                str(self.component_path),
                self.digest,
                str(self.policy_path),
                self.policy_digest,
                "zstd/host-build",
                RENDERER["canonical_sha256"](
                    RENDERER["render_component_documents"](
                        json.loads(
                            (REPOSITORY / "config/release.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    )["zstd/host-build"]
                ),
                "host",
                "/usr/bin",
                "-",
                "2",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((prefix / "lib/libzstd.a").is_file())
        self.assertFalse(list(prefix.rglob("*.so")))
        manifest = json.loads(build_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["identity"], "host")
        self.assertTrue(manifest["policy"]["position_independent"])
        self.assertTrue(manifest["policy"]["multithread"])


if __name__ == "__main__":
    unittest.main()

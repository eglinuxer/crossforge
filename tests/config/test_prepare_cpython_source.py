import copy
import io
import json
import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
PREPARER = runpy.run_path(str(REPOSITORY / "scripts/prepare-cpython-source.py"))
PreparationError = PREPARER["PreparationError"]


class PrepareCPythonSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(
            (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
        )

    def test_only_qualified_rows_are_implemented(self):
        self.assertEqual(PREPARER["row_for"](self.config, "cp311")["version"], "3.11.16")
        self.assertEqual(PREPARER["row_for"](self.config, "cp313")["version"], "3.13.15")
        with self.assertRaises(PreparationError):
            PREPARER["row_for"](self.config, "cp312")

    def test_row_adapter_mismatch_is_rejected(self):
        config = copy.deepcopy(self.config)
        config["python"]["versions"][2]["adapter"] = "modern"
        with self.assertRaises(PreparationError):
            PREPARER["row_for"](config, "cp311")

    def test_prepare_is_atomic_and_writes_row_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.11.16.tar.xz"
            self.write_archive(archive)
            config = self.configuration_for(archive)
            destination = directory / "source"
            manifest = directory / "manifest.json"
            identity = PREPARER["prepare"](
                config, "cp311", archive, destination, manifest, REPOSITORY
            )
            self.assertEqual(identity["compact"], "311")
            self.assertEqual(identity["patches"], [])
            self.assertEqual(identity["support"], "security")
            self.assertEqual(
                identity["release_sha256"], PREPARER["canonical_sha256"](config)
            )
            self.assertTrue((destination / "configure").is_file())
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8")), identity)
            with self.assertRaises(PreparationError):
                PREPARER["prepare"](
                    config, "cp311", archive, destination, manifest, REPOSITORY
                )

    def test_manifest_publish_failure_rolls_back_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "Python-3.11.16.tar.xz"
            self.write_archive(archive)
            config = self.configuration_for(archive)
            destination = directory / "source"
            manifest = directory / "manifest.json"
            with mock.patch.object(
                PREPARER["os"], "replace", side_effect=OSError("publish failed")
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    PREPARER["prepare"](
                        config,
                        "cp311",
                        archive,
                        destination,
                        manifest,
                        REPOSITORY,
                    )
            self.assertFalse(destination.exists())
            self.assertFalse(manifest.exists())
            self.assertEqual(list(directory.glob(".*.tmp")), [])

    def test_archive_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe.tar.xz"
            with tarfile.open(str(archive), "w:xz") as output:
                root = tarfile.TarInfo("Python-3.11.16")
                root.type = tarfile.DIRTYPE
                output.addfile(root)
                link = tarfile.TarInfo("Python-3.11.16/configure")
                link.type = tarfile.SYMTYPE
                link.linkname = "/bin/true"
                output.addfile(link)
            extraction = Path(temporary) / "extract"
            extraction.mkdir()
            with self.assertRaises(PreparationError):
                PREPARER["extract_archive"](archive, extraction, "3.11.16")

    @staticmethod
    def write_archive(path):
        with tarfile.open(str(path), "w:xz") as output:
            root = tarfile.TarInfo("Python-3.11.16")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            output.addfile(root)
            payload = b"#!/bin/sh\nexit 0\n"
            configure = tarfile.TarInfo("Python-3.11.16/configure")
            configure.mode = 0o755
            configure.size = len(payload)
            output.addfile(configure, io.BytesIO(payload))

    @staticmethod
    def configuration_for(archive):
        digest, size = PREPARER["sha256_file"](archive)
        return {
            "python": {
                "versions": [
                    {
                        "version": "3.11.16",
                        "adapter": "transition",
                        "support": "security",
                        "source": {
                            "status": "locked",
                            "url": "https://example.invalid/Python-3.11.16.tar.xz",
                            "sha256": digest,
                            "size": size,
                        },
                        "patches": [],
                    }
                ]
            }
        }


if __name__ == "__main__":
    unittest.main()

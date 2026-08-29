import hashlib
import os
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
MATERIALIZER = runpy.run_path(str(REPOSITORY / "scripts/materialize-sysroot.py"))
SOURCE_FETCHER = runpy.run_path(str(REPOSITORY / "scripts/fetch-release-source.py"))


def package_for(payload):
    return {
        "location": "Packages/f/fake-1-1.x86_64.rpm",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class MaterializeSysrootTests(unittest.TestCase):
    def test_bundle_requires_exact_files_and_content(self):
        payload = b"locked-rpm"
        lock = {"packages": [package_for(payload)]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rpm = directory / "fake-1-1.x86_64.rpm"
            rpm.write_bytes(payload)
            MATERIALIZER["verify_bundle"](lock, directory)

            rpm.write_bytes(payload + b"changed")
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["verify_bundle"](lock, directory)

    def test_bundle_rejects_unlocked_rpm(self):
        payload = b"locked-rpm"
        lock = {"packages": [package_for(payload)]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "fake-1-1.x86_64.rpm").write_bytes(payload)
            (directory / "extra.rpm").write_bytes(b"extra")
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["verify_bundle"](lock, directory)

    def test_download_rejects_dangling_destination_symlink(self):
        payload = b"locked-rpm"
        package = package_for(payload)
        package["url"] = "https://example.invalid/fake.rpm"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            outside = directory.parent / (directory.name + "-outside")
            destination = directory / "fake-1-1.x86_64.rpm"
            destination.symlink_to(outside)
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["download_package"](package, directory)
            self.assertFalse(outside.exists())

    def test_usrmerge_links_are_derived_from_filesystem_manifest(self):
        manifest = "\n".join(
            "%s 1 0 0 0120777 root root 0 0 0 %s" % item
            for item in (
                ("/bin", "usr/bin"),
                ("/lib", "usr/lib"),
                ("/lib64", "usr/lib64"),
                ("/sbin", "usr/sbin"),
            )
        )
        original_command = MATERIALIZER["preseed_usrmerge_symlinks"].__globals__[
            "command"
        ]
        MATERIALIZER["preseed_usrmerge_symlinks"].__globals__["command"] = (
            lambda _arguments, _label: manifest
        )
        try:
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "sysroot"
                bundle = Path(temporary) / "rpms"
                destination.mkdir()
                bundle.mkdir()
                lock = {
                    "packages": [
                        {
                            "name": "filesystem",
                            "location": "Packages/f/filesystem.rpm",
                        }
                    ]
                }
                MATERIALIZER["preseed_usrmerge_symlinks"](
                    lock, bundle, destination
                )
                self.assertEqual(os.readlink(str(destination / "lib64")), "usr/lib64")
                self.assertTrue((destination / "usr/lib64").is_dir())
        finally:
            MATERIALIZER["preseed_usrmerge_symlinks"].__globals__["command"] = (
                original_command
            )

    def test_source_fetch_rejects_dangling_output_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            outside = directory.parent / (directory.name + "-source-outside")
            output = directory / "source.rpm"
            output.symlink_to(outside)
            source = {
                "url": "https://example.invalid/source.rpm",
                "size": 1,
                "sha256": "0" * 64,
            }
            with self.assertRaises(SOURCE_FETCHER["ValidationError"]):
                SOURCE_FETCHER["fetch"](source, output)
            self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()

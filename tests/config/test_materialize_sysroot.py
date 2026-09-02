import hashlib
import io
import os
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
MATERIALIZER = runpy.run_path(str(REPOSITORY / "scripts/materialize-sysroot.py"))
SOURCE_FETCHER = runpy.run_path(str(REPOSITORY / "scripts/fetch-release-source.py"))


def package_for(payload):
    return {
        "filename": "fake-1-1.x86_64.rpm",
        "item": {
            "name": "fake",
            "location": "Packages/f/fake-1-1.x86_64.rpm",
            "url": "https://example.invalid/fake.rpm",
            "size": len(payload),
        },
        "lock": {"received_sha256": hashlib.sha256(payload).hexdigest()},
    }


def context_for(payload, role="target-sysroot"):
    component = (
        "rpm/sysroot-x86_64"
        if role == "target-sysroot"
        else "rpm/%s" % role
    )
    return {
        "role": role,
        "packages": [package_for(payload)],
        "result_packages": ["fake-0:1-1.x86_64"],
        "release_binding": {
            "kind": "release-component",
            "component": component,
            "scope": "build",
            "canonical_sha256": "c" * 64,
        },
    }


class MaterializeSysrootTests(unittest.TestCase):
    def test_bundle_requires_exact_files_and_content(self):
        payload = b"locked-rpm"
        context = context_for(payload)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rpm = directory / "fake-1-1.x86_64.rpm"
            rpm.write_bytes(payload)
            MATERIALIZER["verify_bundle"](context, directory)

            rpm.write_bytes(payload + b"changed")
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["verify_bundle"](context, directory)

    def test_bundle_rejects_unlocked_rpm(self):
        payload = b"locked-rpm"
        context = context_for(payload)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "fake-1-1.x86_64.rpm").write_bytes(payload)
            (directory / "extra.rpm").write_bytes(b"extra")
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["verify_bundle"](context, directory)

    def test_download_rejects_dangling_destination_symlink(self):
        payload = b"locked-rpm"
        package = package_for(payload)
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
            lambda _arguments, _label: (manifest, "")
        )
        try:
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "sysroot"
                bundle = Path(temporary) / "rpms"
                destination.mkdir()
                bundle.mkdir()
                context = {
                    "packages": [
                        {
                            "filename": "filesystem.rpm",
                            "item": {"name": "filesystem"},
                        }
                    ]
                }
                MATERIALIZER["preseed_usrmerge_symlinks"](
                    context, bundle, destination
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

    def test_normalize_binds_forward_item_to_verified_package(self):
        digest = "a" * 64
        fingerprint = "b" * 40
        item = {
            "name": "fake",
            "epoch": 0,
            "version": "1",
            "release": "1",
            "arch": "x86_64",
            "nevra": "fake-0:1-1.x86_64",
            "repo_id": "baseos",
            "action": "install",
            "reason": "user",
            "location": "Packages/f/fake-1-1.x86_64.rpm",
            "url": "https://example.invalid/fake.rpm",
            "repository_checksum": {"algorithm": "sha256", "value": digest},
            "size": 1,
            "install_size": 1,
            "source_rpm": "fake-1-1.src.rpm",
        }
        header = {
            key: item[key]
            for key in (
                "name",
                "epoch",
                "version",
                "release",
                "arch",
                "nevra",
                "source_rpm",
            )
        }
        lock = {
            "packages": [
                {
                    "nevra": item["nevra"],
                    "received_sha256": digest,
                    "header": header,
                    "signature": {
                        "status": "verified",
                        "fingerprint": fingerprint,
                    },
                }
            ]
        }
        transaction = {
            "identity": {"role": "target-sysroot", "arch": "x86_64"},
            "repositories": [
                {
                    "id": "baseos",
                    "gpg_key": {
                        "sha256": "c" * 64,
                        "fingerprint": fingerprint,
                    },
                }
            ],
            "items": [item],
            "manifests": {"result": {"packages": [item["nevra"]]}},
        }
        context = MATERIALIZER["normalize_lock"](lock, transaction)
        self.assertEqual(context["role"], "target-sysroot")
        self.assertEqual(context["packages"][0]["filename"], "fake-1-1.x86_64.rpm")
        self.assertEqual(context["result_packages"], [item["nevra"]])

    def test_install_rejects_host_lock_before_mutating_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "sysroot"
            with self.assertRaises(MATERIALIZER["ValidationError"]):
                MATERIALIZER["install"](
                    context_for(b"rpm", role="host-build-common"),
                    root / "bundle",
                    root / "key",
                    destination,
                )
            self.assertFalse(destination.exists())

    def test_root_inventory_is_canonicalized(self):
        original = MATERIALIZER["root_inventory"].__globals__["command"]
        MATERIALIZER["root_inventory"].__globals__["command"] = (
            lambda _arguments, _label: (
                "zlib-0:1-1.x86_64\nbash-0:1-1.x86_64\n",
                "",
            )
        )
        try:
            self.assertEqual(
                MATERIALIZER["root_inventory"](Path("/sysroot")),
                ["bash-0:1-1.x86_64", "zlib-0:1-1.x86_64"],
            )
        finally:
            MATERIALIZER["root_inventory"].__globals__["command"] = original

    def test_target_install_uses_offline_flags_and_embeds_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "fake-1-1.x86_64.rpm").write_bytes(b"rpm")
            destination = root / "sysroot"
            key = root / "key"
            key.write_bytes(b"key")
            package = package_for(b"rpm")
            context = {
                "role": "target-sysroot",
                "arch": "x86_64",
                "packages": [package],
                "result_packages": ["fake-0:1-1.x86_64"],
                "lock": {"kind": "rpm-lock"},
                "transaction": {"kind": "rpm-transaction"},
                "release_binding": {
                    "kind": "release-component",
                    "component": "rpm/sysroot-x86_64",
                    "scope": "build",
                    "canonical_sha256": "c" * 64,
                },
            }
            calls = []
            globals_ = MATERIALIZER["install"].__globals__
            originals = {
                name: globals_[name]
                for name in (
                    "verify_bundle",
                    "verify_key_and_headers",
                    "require_empty_destination",
                    "preseed_usrmerge_symlinks",
                    "command",
                    "root_inventory",
                )
            }

            def fake_command(arguments, _label):
                calls.append([str(argument) for argument in arguments])
                return "", ""

            globals_["verify_bundle"] = lambda _context, _bundle: None
            globals_["verify_key_and_headers"] = (
                lambda _context, _bundle, _key: [package]
            )
            globals_["require_empty_destination"] = lambda _destination: destination
            globals_["preseed_usrmerge_symlinks"] = (
                lambda _context, _bundle, _destination: None
            )
            globals_["command"] = fake_command
            globals_["root_inventory"] = lambda _destination: context[
                "result_packages"
            ]
            try:
                for path in (
                    "usr/include/features.h",
                    "usr/lib64/crt1.o",
                    "usr/lib64/libc.so",
                    "usr/lib64/libstdc++.so.6",
                    "lib64/libgcc_s.so.1",
                ):
                    target = destination / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                with redirect_stdout(io.StringIO()):
                    MATERIALIZER["install"](context, bundle, key, destination)
            finally:
                for name, value in originals.items():
                    globals_[name] = value

            transaction_call = [call for call in calls if "-U" in call]
            self.assertEqual(len(transaction_call), 1)
            self.assertIn("--root", transaction_call[0])
            self.assertIn("--noscripts", transaction_call[0])
            self.assertIn("--notriggers", transaction_call[0])
            metadata = destination / "usr/share/crossforge"
            self.assertTrue((metadata / "sysroot-lock.json").is_file())
            self.assertTrue((metadata / "sysroot-transaction.json").is_file())
            self.assertTrue(
                (metadata / "sysroot-release-binding.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()

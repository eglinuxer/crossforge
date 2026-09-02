import copy
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
FINALIZER = runpy.run_path(
    str(REPOSITORY / "scripts/finalize-cpython-qualification.py")
)
TARGET = "x86_64-unknown-linux-gnu"
VERSION = "3.13.15"
PYTHON_SHA256 = "1" * 64
EXTENSION_SHA256 = "2" * 64
SYSROOT_SHA256 = "3" * 64
SYSROOT_TRANSACTION_SHA256 = "e" * 64


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class PythonQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.release_path = self.directory / "release.json"
        self.compile_path = self.directory / "compile.json"
        self.locked_path = self.directory / "locked.json"
        self.clean_path = self.directory / "clean.json"

        self.release = {
            "schema_version": 1,
            "base_image": {
                "digest": "sha256:" + "a" * 64,
                "manifests": {
                    "amd64": "sha256:" + "b" * 64,
                    "arm64": "sha256:" + "c" * 64,
                },
            },
            "qemu": {
                "version": "10.2.3",
                "executor": {
                    "binary_sha256": "d" * 64,
                    "cpu": "cortex-a53",
                    "uname_release": "4.18.0",
                },
            },
            "targets": [
                {
                    "triple": TARGET,
                    "sysroot": {
                        "status": "locked",
                        "canonical_sha256": SYSROOT_SHA256,
                    },
                }
            ],
            "python": {
                "versions": [
                    {
                        "version": VERSION,
                        "source": {
                            "status": "locked",
                            "url": "https://www.python.org/ftp/python/3.13.15/Python-3.13.15.tar.xz",
                            "size": 23160540,
                            "sha256": "4" * 64,
                            "sigstore": {
                                "bundle_sha256": "5" * 64,
                                "verification": "archived-unverified",
                            },
                        },
                    }
                ]
            },
        }
        self.write_json(self.release_path, self.release)
        self.compile = self.valid_compile()
        self.write_json(self.compile_path, self.compile)
        self.locked = self.valid_runtime("locked-sysroot")
        self.clean = self.valid_runtime("clean-rocky")
        self.write_json(self.locked_path, self.locked)
        self.write_json(self.clean_path, self.clean)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def valid_compile(self):
        source = self.release["python"]["versions"][0]["source"]
        return {
            "qualification_schema_version": 1,
            "report_kind": "crossforge-cpython-compile",
            "target": TARGET,
            "version": VERSION,
            "release_sha256": canonical_sha256(self.release),
            "source": {
                "url": source["url"],
                "size": source["size"],
                "sha256": source["sha256"],
                "sigstore_bundle_sha256": source["sigstore"]["bundle_sha256"],
                "sigstore_verification": "archived-unverified",
            },
            "sysroot_sha256": SYSROOT_SHA256,
            "sysroot_transaction_sha256": SYSROOT_TRANSACTION_SHA256,
            "target_prefix": "/opt/crossforge/python/cp313/targets/" + TARGET,
            "build_python": {
                "path": "/opt/crossforge/python/cp313/build/bin/python3.13",
                "version": VERSION,
                "sha256": "6" * 64,
            },
            "target_exec_guard": {
                "canary_observed": True,
                "denied_attempts": 1,
                "canonical_sha256": "0" * 64,
            },
            "python_sha256": PYTHON_SHA256,
            "extension": {
                "name": "_crossforge.cpython-313-x86_64-linux-gnu.so",
                "sha256": EXTENSION_SHA256,
            },
            "required_modules": {
                name: "lib/python3.13/lib-dynload/%s.cpython-313-x86_64-linux-gnu.so"
                % name
                for name in sorted(FINALIZER["REQUIRED_MODULES"])
            },
            "sysconfig": {
                "EXT_SUFFIX": ".cpython-313-x86_64-linux-gnu.so",
                "HOST_GNU_TYPE": TARGET,
            },
            "sdk_tree": {"entries": 100, "canonical_sha256": "7" * 64},
            "elf_audit": {
                "bin/python3.13": {
                    "needed": ["libc.so.6"],
                    "required_versions": {"GLIBC": "2.28"},
                    "sha256": PYTHON_SHA256,
                },
                "qualification/_crossforge.so": {
                    "needed": [],
                    "required_versions": {},
                    "sha256": EXTENSION_SHA256,
                },
                **{
                    relative: {
                        "needed": ["libc.so.6"],
                        "required_versions": {"GLIBC": "2.28"},
                        "sha256": ("%064x" % (index + 16)),
                    }
                    for index, relative in enumerate(
                        {
                            name: "lib/python3.13/lib-dynload/%s.cpython-313-x86_64-linux-gnu.so"
                            % name
                            for name in sorted(FINALIZER["REQUIRED_MODULES"])
                        }.values()
                    )
                },
            },
        }

    def valid_overlay(self):
        packages = [
            {
                "name": name,
                "nevra": "%s-0:1-1.el8.x86_64" % name,
                "received_sha256": "%064x" % (index + 32),
            }
            for index, name in enumerate(FINALIZER["RUNTIME_PACKAGE_NAMES"])
        ]
        identity = {
            "base_image": {
                "index_digest": self.release["base_image"]["digest"],
                "manifest_digest": self.release["base_image"]["manifests"]["amd64"],
            },
            "release_sha256": canonical_sha256(self.release),
            "target": {"arch": "x86_64", "triple": TARGET},
            "sysroot": {
                "lock_sha256": SYSROOT_SHA256,
                "transaction_sha256": SYSROOT_TRANSACTION_SHA256,
            },
            "selected_packages": packages,
            "selected_packages_sha256": canonical_sha256(packages),
        }
        return {
            "schema_version": 1,
            "kind": "crossforge-python-runtime-overlay",
            "qualification_only": True,
            "identity": identity,
            "identity_sha256": canonical_sha256(identity),
            "runtime_inventory": {
                "before_sha256": "8" * 64,
                "before_item_count": 10,
                "after_sha256": "9" * 64,
                "after_item_count": 17,
                "installed_nevras": sorted(
                    package["nevra"] for package in packages
                ),
                "os_release_sha256": "9" * 64,
            },
        }

    def valid_runtime(self, tier):
        compile_sha256 = hashlib.sha256(self.compile_path.read_bytes()).hexdigest()
        probe_sysconfig = {
            "arch": "x86_64",
            "build_gnu_type": "x86_64-pc-linux-gnu",
            "cache_tag": "cpython-313",
            "cc": "/opt/crossforge/targets/%s/bin/%s-gcc --sysroot=/opt/crossforge/sysroots/el8/x86_64"
            % (TARGET, TARGET),
            "ext_suffix": ".cpython-313-x86_64-linux-gnu.so",
            "host_gnu_type": TARGET,
            "multiarch": "x86_64-linux-gnu",
            "platform": "linux-x86_64",
            "prefix": "/opt/crossforge/python/cp313/targets/" + TARGET,
            "soabi": "cpython-313-x86_64-linux-gnu",
        }
        overlay = self.valid_overlay() if tier == "clean-rocky" else None
        runtime_root = "/runtime-locked" if tier == "locked-sysroot" else "/runtime-clean"
        loaded_objects = sorted(
            runtime_root + "/usr/lib64/" + name
            for name in (
                "libbz2.so.1",
                "libcrypto.so.1.1",
                "libffi.so.6",
                "liblzma.so.5",
                "libsqlite3.so.0",
                "libssl.so.1.1",
                "libuuid.so.1",
                "libz.so.1",
            )
        )
        return {
            "qualification_schema_version": 1,
            "report_kind": "crossforge-cpython-runtime",
            "target": TARGET,
            "version": VERSION,
            "tier": tier,
            "status": "passed",
            "release_sha256": canonical_sha256(self.release),
            "compile_report_sha256": compile_sha256,
            "python_sha256": PYTHON_SHA256,
            "extension_sha256": EXTENSION_SHA256,
            "probe_sha256": "f" * 64,
            "runtime": {
                "kind": "locked-sysroot" if tier == "locked-sysroot" else "clean-rocky-overlay",
                "identity_sha256": (
                    SYSROOT_SHA256
                    if tier == "locked-sysroot"
                    else overlay["identity_sha256"]
                ),
                "os_release_sha256": "9" * 64,
                "loader_sha256": "a" * 64,
                "overlay_evidence": overlay,
            },
            "executor": {
                "kind": "native-chroot",
                "binary_sha256": None,
                "version": None,
                "cpu": None,
                "uname_release": None,
            },
            "loader_dependencies": [
                "/lib64/ld-linux-x86-64.so.2",
                "libc.so.6 => /lib64/libc.so.6",
            ],
            "device_loader_dependencies": [
                runtime_root + "/usr/lib64/ld-linux-x86-64.so.2",
                "libc.so.6 => " + runtime_root + "/usr/lib64/libc.so.6",
            ],
            "device_loaded_objects": loaded_objects,
            "probe": {
                "schema_version": 1,
                "report_kind": "crossforge-cpython-probe",
                "mode": "core",
                "status": "passed",
                "target": TARGET,
                "version": VERSION,
                "sysconfig": copy.deepcopy(probe_sysconfig),
                "imports": copy.deepcopy(FINALIZER["REQUIRED_PROBE_IMPORTS"]),
                "functionality": {
                    "compression_roundtrips": ["bz2", "lzma", "zlib"],
                    "ctypes_strlen": 10,
                    "hashlib_sha256": "822da7168e47d27301f5c747b5e678f593d60dc700049d33d3d3e1381dac1630",
                    "openssl": "OpenSSL 1.1.1",
                    "sqlite": "3.26.0",
                    "uuid5": "d2222479-a666-5841-bee6-944f95190b64",
                },
                "extension": {
                    "answer": 42,
                    "file": "_crossforge.cpython-313-x86_64-linux-gnu.so",
                    "module": "_crossforge",
                },
                "hash_algorithm": {
                    "algorithm": "siphash13",
                    "hash_bits": 64,
                    "seed_bits": 128,
                },
                "threading": {"event": True, "result": 5050},
                "semaphore": {"acquire_release": True, "get_value": True},
                "network": {"address": "127.0.0.1", "family": "AF_INET", "port": 443},
                "timezone": {"posix_rule": True, "tzset": True, "utc_epoch": True},
                "wchar": {"code_points": 17, "cpython_api": True, "wchar_bytes": 4},
            },
            "device_probe": {
                "schema_version": 1,
                "report_kind": "crossforge-cpython-probe",
                "mode": "devices",
                "status": "passed",
                "target": TARGET,
                "version": VERSION,
                "sysconfig": copy.deepcopy(probe_sysconfig),
                "probe": {
                    "pty": {
                        "character_devices": True,
                        "isatty": True,
                        "roundtrip_sha256": "8d6d22b3644e6c07099e253b687957c6beeea318c584f575877b571a87af5a53",
                    }
                },
            },
        }

    def finalize(self):
        return FINALIZER["finalize"](
            self.compile_path,
            self.locked_path,
            self.clean_path,
            self.release_path,
            TARGET,
            VERSION,
        )

    def refresh_runtime_bindings(self):
        digest = hashlib.sha256(self.compile_path.read_bytes()).hexdigest()
        for path, report in (
            (self.locked_path, self.locked),
            (self.clean_path, self.clean),
        ):
            report["compile_report_sha256"] = digest
            self.write_json(path, report)

    def test_final_report_binds_static_and_both_runtime_tiers(self):
        report = self.finalize()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["report_kind"], "crossforge-cpython-qualification")
        self.assertEqual(report["python_sha256"], PYTHON_SHA256)
        self.assertEqual(report["extension_sha256"], EXTENSION_SHA256)
        self.assertEqual(
            set(report["executions"]), {"locked-sysroot", "clean-rocky"}
        )
        self.assertEqual(
            json.dumps(report, sort_keys=True),
            json.dumps(self.finalize(), sort_keys=True),
        )

    def test_duplicate_json_key_is_rejected(self):
        duplicate = self.directory / "duplicate.json"
        duplicate.write_text('{"status":"passed","status":"passed"}\n', encoding="utf-8")
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "duplicate JSON key"):
            FINALIZER["load_json"](duplicate)

    def test_unknown_compile_field_is_rejected(self):
        self.compile["surprise"] = True
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "unknown field"):
            self.finalize()

    def test_missing_runtime_field_is_rejected(self):
        del self.clean["device_probe"]
        self.write_json(self.clean_path, self.clean)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "missing field"):
            self.finalize()

    def test_unknown_nested_runtime_field_is_rejected(self):
        self.locked["runtime"]["surprise"] = "no"
        self.write_json(self.locked_path, self.locked)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "unknown field"):
            self.finalize()

    def test_release_digest_mismatch_is_rejected(self):
        self.release["schema_version"] = 2
        self.write_json(self.release_path, self.release)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "release digest mismatch"):
            self.finalize()

    def test_source_mismatch_is_rejected(self):
        self.compile["source"]["sha256"] = "f" * 64
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "source differs"):
            self.finalize()

    def test_sysroot_mismatch_is_rejected(self):
        self.compile["sysroot_sha256"] = "f" * 64
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "sysroot digest mismatch"):
            self.finalize()

    def test_target_and_version_mismatches_are_rejected(self):
        self.compile["target"] = "aarch64-unknown-linux-gnu"
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "target mismatch"):
            self.finalize()

        self.compile = self.valid_compile()
        self.compile["version"] = "3.13.14"
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "version mismatch"):
            self.finalize()

    def test_runtime_artifact_hash_mismatch_is_rejected(self):
        self.clean["extension_sha256"] = "f" * 64
        self.write_json(self.clean_path, self.clean)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "extension_sha256 mismatch"):
            self.finalize()

    def test_runtime_cannot_substitute_another_compile_report(self):
        self.compile["sdk_tree"]["entries"] = 101
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"], "compile_report_sha256 mismatch"
        ):
            self.finalize()

    def test_locked_runtime_identity_must_be_the_selected_sysroot(self):
        self.locked["runtime"]["identity_sha256"] = "f" * 64
        self.write_json(self.locked_path, self.locked)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "locked sysroot"):
            self.finalize()

    def test_loader_dependencies_must_be_canonical(self):
        self.locked["loader_dependencies"].reverse()
        self.write_json(self.locked_path, self.locked)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "not canonical"):
            self.finalize()

    def test_probe_semantics_cannot_be_relabelled_passed(self):
        mutations = (
            ("imports", ["forged"]),
            ("extension", {"answer": 0, "file": "_crossforge.so", "module": "_crossforge"}),
            ("threading", {"event": False, "result": 0}),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                report = copy.deepcopy(self.locked)
                report["probe"][field] = value
                self.write_json(self.locked_path, report)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()
                self.write_json(self.locked_path, self.locked)

    def test_clean_overlay_identity_is_fully_bound(self):
        self.clean["runtime"]["overlay_evidence"]["identity"]["base_image"][
            "manifest_digest"
        ] = "sha256:" + "f" * 64
        self.write_json(self.clean_path, self.clean)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "base image"):
            self.finalize()

    def test_required_module_must_be_in_elf_audit(self):
        relative = next(iter(self.compile["required_modules"].values()))
        del self.compile["elf_audit"][relative]
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "ELF audit"):
            self.finalize()

    def test_nested_compile_contract_is_strict(self):
        self.compile["source"]["signature"] = "unexpected"
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "unknown field"):
            self.finalize()

    def test_x86_runtime_must_use_native_chroot(self):
        self.clean["executor"]["version"] = "different"
        self.write_json(self.clean_path, self.clean)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "native chroot"):
            self.finalize()

    def test_aarch64_runtime_binds_the_release_qemu(self):
        target = "aarch64-unknown-linux-gnu"
        report = copy.deepcopy(self.locked)
        report["target"] = target
        report["executor"] = {
            "kind": "explicit-qemu",
            "binary_sha256": self.release["qemu"]["executor"]["binary_sha256"],
            "version": self.release["qemu"]["version"],
            "cpu": self.release["qemu"]["executor"]["cpu"],
            "uname_release": self.release["qemu"]["executor"]["uname_release"],
        }
        for name in ("probe", "device_probe"):
            report[name]["target"] = target
            report[name]["sysconfig"].update(
                {
                    "arch": "aarch64",
                    "cc": "/opt/crossforge/targets/%s/bin/%s-gcc --sysroot=/opt/crossforge/sysroots/el8/aarch64"
                    % (target, target),
                    "ext_suffix": ".cpython-313-aarch64-linux-gnu.so",
                    "host_gnu_type": target,
                    "multiarch": "aarch64-linux-gnu",
                    "platform": "linux-aarch64",
                    "prefix": "/opt/crossforge/python/cp313/targets/" + target,
                    "soabi": "cpython-313-aarch64-linux-gnu",
                }
            )
        context = {
            "release": self.release,
            "release_sha256": canonical_sha256(self.release),
            "sysroot_sha256": SYSROOT_SHA256,
        }
        compile_report = {
            "python_sha256": PYTHON_SHA256,
            "extension": {"sha256": EXTENSION_SHA256},
        }
        parsed = FINALIZER["validate_runtime_result"](
            report,
            "locked-sysroot",
            context,
            compile_report,
            report["compile_report_sha256"],
            target,
            VERSION,
        )
        self.assertEqual(parsed["executor"]["kind"], "explicit-qemu")

        report["executor"]["binary_sha256"] = "f" * 64
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "binary_sha256 mismatch"):
            FINALIZER["validate_runtime_result"](
                report,
                "locked-sysroot",
                context,
                compile_report,
                report["compile_report_sha256"],
                target,
                VERSION,
            )


if __name__ == "__main__":
    unittest.main()

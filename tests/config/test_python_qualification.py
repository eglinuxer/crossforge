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
QUALIFIER = runpy.run_path(str(REPOSITORY / "scripts/qualify-cpython.py"))
ROW_CONTRACT = runpy.run_path(str(REPOSITORY / "scripts/python_row_contract.py"))
RELEASE_CONFIG = json.loads(
    (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
)
TARGET = "x86_64-unknown-linux-gnu"
IMPLEMENTED_VERSIONS = tuple(
    next(
        entry["version"]
        for entry in RELEASE_CONFIG["python"]["versions"]
        if entry["version"].rsplit(".", 1)[0] == record["minor"]
    )
    for record in ROW_CONTRACT["IMPLEMENTED_ROWS"]
)
VERSION = next(
    version for version in IMPLEMENTED_VERSIONS if version.startswith("3.13.")
)
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

        self.reset_fixture(VERSION)

    def reset_fixture(self, version):
        self.version = version
        self.contract = ROW_CONTRACT["contract_for_version"](version)
        self.minor = self.contract["minor"]
        self.compact = self.minor.replace(".", "")
        self.row = self.contract["row"]
        self.adapter = self.contract["adapter"]
        canonical_entry = next(
            entry
            for entry in RELEASE_CONFIG["python"]["versions"]
            if entry["version"] == self.version
        )

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
                        "version": self.version,
                        "adapter": self.adapter,
                        "source": {
                            "status": "locked",
                            "url": "https://www.python.org/ftp/python/%s/Python-%s.tar.xz"
                            % (self.version, self.version),
                            "size": canonical_entry["source"]["size"],
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
        build_directory = "/work/build/cpython-%s-x86_64" % self.row
        executable_canary = build_directory + "/target-exec-canary"
        loader_canary = build_directory + "/target-dlopen-canary.so"
        guard_records = [
            {"operation": operation, "path": executable_canary}
            for operation in FINALIZER["EXEC_OPERATIONS"]
        ] + [
            {"operation": operation, "path": loader_canary}
            for operation in FINALIZER["LOADER_OPERATIONS"]
        ]
        report = {
            "qualification_schema_version": 2,
            "report_kind": "crossforge-cpython-compile",
            "target": TARGET,
            "version": self.version,
            "adapter": self.adapter,
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
            "target_prefix": "/opt/crossforge/python/%s/targets/%s"
            % (self.row, TARGET),
            "build_python": {
                "path": "/opt/crossforge/python/%s/build/bin/python%s"
                % (self.row, self.minor),
                "version": self.version,
                "sha256": "6" * 64,
                "sdk_tree": {"entries": 200, "canonical_sha256": "d" * 64},
            },
            "target_artifact_guard": {
                "execution_canaries": list(FINALIZER["EXEC_OPERATIONS"]),
                "loader_canaries": list(FINALIZER["LOADER_OPERATIONS"]),
                "records": guard_records,
                "denied_execution_attempts": len(FINALIZER["EXEC_OPERATIONS"]),
                "denied_loader_attempts": len(FINALIZER["LOADER_OPERATIONS"]),
                "canonical_sha256": canonical_sha256(guard_records),
            },
            "python_sha256": PYTHON_SHA256,
            "extension": {
                "name": "_crossforge.cpython-%s-x86_64-linux-gnu.so"
                % self.compact,
                "sha256": EXTENSION_SHA256,
            },
            "required_modules": {
                name: "lib/python%s/lib-dynload/%s.cpython-%s-x86_64-linux-gnu.so"
                % (self.minor, name, self.compact)
                for name in sorted(FINALIZER["REQUIRED_MODULES"])
            },
            "sysconfig": {
                "EXT_SUFFIX": ".cpython-%s-x86_64-linux-gnu.so" % self.compact,
                "HAVE_ALIGNED_REQUIRED": 0,
                "HAVE_USABLE_WCHAR_T": 0,
                "HOST_GNU_TYPE": TARGET,
                "MULTIARCH": "x86_64-linux-gnu",
                "Py_DEBUG": 0,
                "SIZEOF_WCHAR_T": 4,
                "SOABI": "cpython-%s-x86_64-linux-gnu" % self.compact,
            },
            "sdk_tree": {"entries": 100, "canonical_sha256": "7" * 64},
            "elf_audit": {
                "bin/python%s" % self.minor: {
                    "needed": ["libc.so.6"],
                    "required_versions": {"GLIBC": "2.28"},
                    "sha256": PYTHON_SHA256,
                },
                "qualification/_crossforge.cpython-%s-x86_64-linux-gnu.so"
                % self.compact: {
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
                            name: "lib/python%s/lib-dynload/%s.cpython-%s-x86_64-linux-gnu.so"
                            % (self.minor, name, self.compact)
                            for name in sorted(FINALIZER["REQUIRED_MODULES"])
                        }.values()
                    )
                },
            },
        }
        if self.contract["gil_policy"] == "zero":
            report["sysconfig"]["Py_GIL_DISABLED"] = 0
        return report

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
            "cache_tag": "cpython-%s" % self.compact,
            "cc": "/opt/crossforge/targets/%s/bin/%s-gcc --sysroot=/opt/crossforge/sysroots/el8/x86_64"
            % (TARGET, TARGET),
            "ext_suffix": ".cpython-%s-x86_64-linux-gnu.so" % self.compact,
            "host_gnu_type": TARGET,
            "multiarch": "x86_64-linux-gnu",
            "platform": "linux-x86_64",
            "prefix": "/opt/crossforge/python/%s/targets/%s" % (self.row, TARGET),
            "soabi": "cpython-%s-x86_64-linux-gnu" % self.compact,
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
            "qualification_schema_version": 2,
            "report_kind": "crossforge-cpython-runtime",
            "target": TARGET,
            "version": self.version,
            "adapter": self.adapter,
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
                "schema_version": 2,
                "report_kind": "crossforge-cpython-probe",
                "mode": "core",
                "status": "passed",
                "target": TARGET,
                "version": self.version,
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
                    "file": "_crossforge.cpython-%s-x86_64-linux-gnu.so"
                    % self.compact,
                    "module": "_crossforge",
                },
                "hash_algorithm": {
                    "algorithm": "siphash13",
                    "hash_bits": 64,
                    "seed_bits": 128,
                },
                "threading": {"event": True, "result": 5050},
                "semaphore": {
                    "multiprocessing_lock": True,
                    "unnamed_acquire_release": True,
                    "unnamed_get_value": True,
                },
                "network": {"address": "127.0.0.1", "family": "AF_INET", "port": 443},
                "timezone": {"posix_rule": True, "tzset": True, "utc_epoch": True},
                "wchar": {"code_points": 17, "cpython_api": True, "wchar_bytes": 4},
            },
            "device_probe": {
                "schema_version": 2,
                "report_kind": "crossforge-cpython-probe",
                "mode": "devices",
                "status": "passed",
                "target": TARGET,
                "version": self.version,
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
            self.version,
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

    def test_target_artifact_audit_requires_every_dynamic_guard_canary(self):
        build = Path("/work/build/cpython-cp313-x86_64")
        prefix = Path("/opt/crossforge/python/cp313/targets/" + TARGET)
        executable = str(build / "target-exec-canary")
        shared_object = str(build / "target-dlopen-canary.so")
        lines = [
            operation + "\t" + executable
            for operation in QUALIFIER["EXEC_OPERATIONS"]
        ] + [
            operation + "\t" + shared_object
            for operation in QUALIFIER["LOADER_OPERATIONS"]
        ]
        result = QUALIFIER["parse_target_artifact_audit"](
            lines, build, prefix
        )
        self.assertEqual(
            result["denied_execution_attempts"],
            len(QUALIFIER["EXEC_OPERATIONS"]),
        )
        self.assertEqual(
            result["denied_loader_attempts"],
            len(QUALIFIER["LOADER_OPERATIONS"]),
        )

        for invalid in (
            lines[1:],
            lines + ["unknown\t" + executable],
            lines + ["dlopen\t" + str(prefix / "lib/python-target.so")],
        ):
            with self.assertRaises(QUALIFIER["QualificationError"]):
                QUALIFIER["parse_target_artifact_audit"](
                    invalid, build, prefix
                )

    def test_all_implemented_rows_use_their_own_abi_adapter_and_gil_policy(self):
        for version in IMPLEMENTED_VERSIONS:
            with self.subTest(version=version):
                self.reset_fixture(version)
                report = self.finalize()
                contract = ROW_CONTRACT["contract_for_version"](version)
                compact = contract["minor"].replace(".", "")

                self.assertEqual(report["adapter"], contract["adapter"])
                self.assertEqual(report["compile"]["adapter"], contract["adapter"])
                self.assertEqual(
                    report["compile"]["target_prefix"],
                    "/opt/crossforge/python/%s/targets/%s"
                    % (contract["row"], TARGET),
                )
                self.assertEqual(
                    report["compile"]["build_python"]["path"],
                    "/opt/crossforge/python/%s/build/bin/python%s"
                    % (contract["row"], contract["minor"]),
                )
                self.assertEqual(
                    report["compile"]["extension"]["name"],
                    "_crossforge.cpython-%s-x86_64-linux-gnu.so" % compact,
                )
                self.assertEqual(
                    report["compile"]["sysconfig"]["SOABI"],
                    "cpython-%s-x86_64-linux-gnu" % compact,
                )
                if contract["gil_policy"] == "absent":
                    self.assertNotIn(
                        "Py_GIL_DISABLED", report["compile"]["sysconfig"]
                    )
                else:
                    self.assertEqual(
                        report["compile"]["sysconfig"]["Py_GIL_DISABLED"], 0
                    )
                for execution in report["executions"].values():
                    self.assertEqual(execution["adapter"], contract["adapter"])
                    self.assertEqual(
                        execution["probe"]["sysconfig"]["cache_tag"],
                        "cpython-%s" % compact,
                    )

    def test_cross_row_adapter_path_soabi_and_gil_mutations_are_rejected(self):
        mutations = (
            (
                "adapter",
                "3.12.14",
                lambda compile_report: compile_report.__setitem__(
                    "adapter", "transition"
                ),
            ),
            (
                "path",
                "3.12.14",
                lambda compile_report: compile_report.__setitem__(
                    "target_prefix", "/opt/crossforge/python/cp313/targets/" + TARGET
                ),
            ),
            (
                "soabi",
                "3.12.14",
                lambda compile_report: compile_report["sysconfig"].__setitem__(
                    "SOABI", "cpython-313-x86_64-linux-gnu"
                ),
            ),
            (
                "312_gil_present",
                "3.12.14",
                lambda compile_report: compile_report["sysconfig"].__setitem__(
                    "Py_GIL_DISABLED", 0
                ),
            ),
            (
                "313_gil_missing",
                "3.13.15",
                lambda compile_report: compile_report["sysconfig"].pop(
                    "Py_GIL_DISABLED"
                ),
            ),
            (
                "313_gil_bool",
                "3.13.15",
                lambda compile_report: compile_report["sysconfig"].__setitem__(
                    "Py_GIL_DISABLED", False
                ),
            ),
            (
                "py_debug_bool",
                "3.12.14",
                lambda compile_report: compile_report["sysconfig"].__setitem__(
                    "Py_DEBUG", False
                ),
            ),
            (
                "wchar_size_float",
                "3.12.14",
                lambda compile_report: compile_report["sysconfig"].__setitem__(
                    "SIZEOF_WCHAR_T", 4.0
                ),
            ),
        )
        for name, version, mutate in mutations:
            with self.subTest(name=name, version=version):
                self.reset_fixture(version)
                mutate(self.compile)
                self.write_json(self.compile_path, self.compile)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()

    def test_static_qualifier_requires_exact_abi_integer_types(self):
        require_abi_value = QUALIFIER["require_abi_value"]
        require_abi_value(0, 0, "Py_DEBUG")
        require_abi_value(4, 4, "SIZEOF_WCHAR_T")
        for actual, expected in ((False, 0), (0.0, 0), (4.0, 4)):
            with self.subTest(actual=actual, expected=expected):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    require_abi_value(actual, expected, "ABI_VALUE")

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
            (
                "semaphore",
                {
                    "multiprocessing_lock": False,
                    "unnamed_acquire_release": True,
                    "unnamed_get_value": True,
                },
            ),
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

    def test_clean_overlay_transaction_is_bound_to_compile_report(self):
        overlay = self.clean["runtime"]["overlay_evidence"]
        overlay["identity"]["sysroot"]["transaction_sha256"] = "a" * 64
        overlay["identity_sha256"] = canonical_sha256(overlay["identity"])
        self.clean["runtime"]["identity_sha256"] = overlay["identity_sha256"]
        self.write_json(self.clean_path, self.clean)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"], "sysroot contract"
        ):
            self.finalize()

    def test_required_module_must_be_in_elf_audit(self):
        relative = next(iter(self.compile["required_modules"].values()))
        del self.compile["elf_audit"][relative]
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "ELF audit"):
            self.finalize()

    def test_required_module_paths_are_unique_and_abi_exact(self):
        shared = next(iter(self.compile["required_modules"].values()))
        self.compile["required_modules"] = {
            name: shared for name in self.compile["required_modules"]
        }
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "module paths"):
            self.finalize()

    def test_elf_version_namespaces_and_ceilings_are_strict(self):
        for versions in ({"GLIBC": "999.0"}, {"EVIL": "1.0"}, {"GLIBC": []}):
            with self.subTest(versions=versions):
                report = copy.deepcopy(self.compile)
                report["elf_audit"]["bin/python3.13"]["required_versions"] = versions
                self.write_json(self.compile_path, report)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()

    def test_guard_records_and_digest_are_revalidated(self):
        self.compile["target_artifact_guard"]["records"][0]["path"] = (
            "/opt/crossforge/python/cp313/targets/"
            + TARGET
            + "/bin/python3.13"
        )
        self.compile["target_artifact_guard"]["canonical_sha256"] = canonical_sha256(
            self.compile["target_artifact_guard"]["records"]
        )
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"], "canary|attempted"
        ):
            self.finalize()

    def test_final_report_revalidates_embedded_evidence(self):
        report = self.finalize()
        report["compile"]["surprise"] = True
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "unknown field"):
            FINALIZER["validate_final_report"](
                report, self.release, TARGET, VERSION
            )

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
        report["probe"]["extension"][
            "file"
        ] = "_crossforge.cpython-313-aarch64-linux-gnu.so"
        context = {
            "release": self.release,
            "adapter": "modern",
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

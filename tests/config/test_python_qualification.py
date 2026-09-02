import ast
import copy
import hashlib
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = str(REPOSITORY / "scripts")
sys.path.insert(0, SCRIPTS)
try:
    RUNTIME_RUNNER = runpy.run_path(
        str(REPOSITORY / "scripts/run-cpython-runtime.py")
    )
    FINALIZER = runpy.run_path(
        str(REPOSITORY / "scripts/finalize-cpython-qualification.py")
    )
    QUALIFIER = runpy.run_path(str(REPOSITORY / "scripts/qualify-cpython.py"))
finally:
    sys.path.remove(SCRIPTS)
ROW_CONTRACT = runpy.run_path(str(REPOSITORY / "scripts/python_row_contract.py"))
RELEASE_CONFIG = json.loads(
    (REPOSITORY / "config/release.json").read_text(encoding="utf-8")
)
TARGET = "x86_64-unknown-linux-gnu"
ABI_CONTEXT = FINALIZER["default_abi_context"](TARGET)
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
ZSTD_VERSION = next(
    version for version in IMPLEMENTED_VERSIONS if version.startswith("3.14.")
)
PYTHON_SHA256 = "1" * 64
EXTENSION_SHA256 = "2" * 64
SYSROOT_TRANSACTION_SHA256 = "e" * 64


def canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def serialized_sha256(value):
    payload = (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def zstd_evidence(required):
    if not required:
        return {
            "available": False,
            "policy": "absent",
            "rejected_imports": ["_zstd", "compression.zstd"],
        }
    return {
        "available": True,
        "corrupt_error": "ZstdError",
        "dictionary": {"finalized": True, "trained": True},
        "multithread": {"nb_workers": 1, "supported": True},
        "payload_sha256": (
            "dd1fc53b1dfcac3378b57b9b8b2723c16f2b6aad628c940b09f6904fba3957a2"
        ),
        "policy": "required",
        "roundtrips": [
            "dictionary",
            "multithread",
            "one-shot",
            "streaming",
            "tarfile",
            "zipfile",
        ],
        "version": "1.5.7",
        "version_info": [1, 5, 7],
    }


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
        self.release = copy.deepcopy(RELEASE_CONFIG)
        selected_target = next(
            item for item in self.release["targets"] if item["triple"] == TARGET
        )
        self.abi_context = copy.deepcopy(ABI_CONTEXT)
        self.sysroot_sha256 = self.abi_context[
            "runtime_provider_evidence"
        ]["sysroot_lock_sha256"]
        selected_target["sysroot"]["canonical_sha256"] = self.sysroot_sha256
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

    def valid_compile_abi(self):
        arch = self.abi_context["arch"]
        catalog = json.loads(
            (
                REPOSITORY
                / (
                    "evidence/abi/el8-%s-python-provider-catalog.json"
                    % arch
                )
            ).read_text(encoding="utf-8")
        )
        catalog_sha256 = canonical_sha256(catalog)
        self.provider_catalog_records = catalog
        if catalog_sha256 != self.abi_context[
            "runtime_provider_evidence"
        ]["provider_catalog_sha256"]:
            raise AssertionError("fixture provider catalog pin mismatch")
        providers = []
        for provider in self.abi_context["expected_providers"]:
            summary = copy.deepcopy(provider)
            summary["elf_record_sha256"] = canonical_sha256(
                catalog[provider["soname"]]
            )
            providers.append(summary)
        python_record = self.valid_elf_record(
            "bin/python%s" % self.minor,
            needed=["libc.so.6"],
            unversioned_exports=["PyFixture_Global"],
        )
        self.python_global_record = python_record
        return dict(
            copy.deepcopy(self.abi_context["expected_identities"]),
            provider_catalog={
                "file": self.abi_context[
                    "reviewed_provider_catalog_file"
                ],
                "provider_count": len(providers),
                "elf_records_sha256": catalog_sha256,
                "records": catalog,
                "providers": providers,
            },
            python_global={
                "identity": "bin/python%s" % self.minor,
                "sha256": PYTHON_SHA256,
                "elf_record_sha256": canonical_sha256(python_record),
                "needed": ["libc.so.6"],
                "default_export_count": 1,
                "record": python_record,
            },
        )

    @staticmethod
    def valid_elf_record(
        identity,
        needed=None,
        soname=None,
        unversioned_exports=None,
    ):
        if needed is None:
            needed = []
        if unversioned_exports is None:
            unversioned_exports = []
        return {
            "identity": identity,
            "soname": soname,
            "needed": list(needed),
            "versioned_exports": [],
            "unversioned_exports": sorted(unversioned_exports),
            "default_exports": sorted(unversioned_exports),
            "versioned_imports": [],
            "unversioned_imports": [],
        }

    def valid_elf_policy(self, artifact, executable=False):
        return {
            "artifact": artifact,
            "profile": "crossforge-qualified-v1",
            "elf_kind": "dynamic-executable" if executable else "shared-object",
            "not_applicable": [] if executable else ["interpreter"],
            "properties": {
                "dynamic_section": True,
                "textrel": False,
                "relr": False,
                "rpath": [],
                "runpath": [],
                "path_tags": [],
                "pie": False,
                "bind_now": True,
                "interpreter": (
                    self.abi_context["interpreter"] if executable else None
                ),
                "gnu_stack_present": True,
                "gnu_stack_executable": False,
                "writable_executable_segments": False,
                "relro": True,
                "machine": self.abi_context["machine"],
                "elf_class": "ELF64",
                "elf_data": "2's complement, little endian",
                "elf_type": "DYN",
            },
            "used_exceptions": [],
        }

    @staticmethod
    def valid_ownership(artifact, needed):
        return {
            "status": "passed",
            "artifact": artifact,
            "needed": list(needed),
            "provider_closure": sorted(set(needed) | {"libc.so.6"}),
            "core_versioned": [],
            "external_versioned": [],
            "strong_unversioned": [],
            "optional_weak": [],
        }

    def valid_elf_audit(self, artifact, sha256, needed=None, executable=False):
        if needed is None:
            needed = ["libc.so.6"]
        if (
            executable
            and hasattr(self, "python_global_record")
            and artifact == self.python_global_record["identity"]
        ):
            record = copy.deepcopy(self.python_global_record)
        else:
            record = self.valid_elf_record(artifact, needed=needed)
        ownership = QUALIFIER["python_abi_audit"].audit_python_elf(
            self.abi_context["baseline"],
            self.abi_context["external_providers"],
            self.provider_catalog_records,
            self.python_global_record,
            record,
        )
        return {
            "needed": list(needed),
            "sha256": sha256,
            "elf_record_sha256": canonical_sha256(record),
            "elf_record": record,
            "elf_policy": self.valid_elf_policy(artifact, executable=executable),
            "ownership": ownership,
        }

    def actual_elf_evidence(self, report=None):
        if report is None:
            report = self.compile
        return {
            name: {
                "sha256": audit["sha256"],
                "elf_record": copy.deepcopy(audit["elf_record"]),
                "elf_policy": copy.deepcopy(audit["elf_policy"]),
            }
            for name, audit in report["elf_audit"].items()
        }

    def valid_compile(self):
        source = next(
            entry["source"]
            for entry in self.release["python"]["versions"]
            if entry["version"] == self.version
        )
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
            "qualification_schema_version": 4,
            "report_kind": "crossforge-cpython-compile",
            "target": TARGET,
            "version": self.version,
            "adapter": self.adapter,
            "release_sha256": canonical_sha256(self.release),
            "qualification_components": QUALIFIER["RELEASE_COMPONENTS"][
                "python_qualification_components"
            ](self.release),
            "source": {
                "url": source["url"],
                "size": source["size"],
                "sha256": source["sha256"],
                "sigstore_bundle_sha256": source["sigstore"]["bundle_sha256"],
                "sigstore_verification": source["sigstore"]["verification"],
            },
            "sysroot_sha256": self.sysroot_sha256,
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
                for name in sorted(
                    set(FINALIZER["REQUIRED_MODULES"])
                    | ({"_zstd"} if self.contract["zstd"] else set())
                )
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
            "elf_audit": {},
            "abi": self.valid_compile_abi(),
            "zstd": self.valid_compile_zstd(),
        }
        python_path = "bin/python%s" % self.minor
        extension_path = (
            "qualification/_crossforge.cpython-%s-x86_64-linux-gnu.so"
            % self.compact
        )
        report["elf_audit"][python_path] = self.valid_elf_audit(
            python_path, PYTHON_SHA256, executable=True
        )
        report["elf_audit"][extension_path] = self.valid_elf_audit(
            extension_path, EXTENSION_SHA256, needed=[]
        )
        for index, relative in enumerate(report["required_modules"].values()):
            report["elf_audit"][relative] = self.valid_elf_audit(
                relative, "%064x" % (index + 16)
            )
        report["elf_audit"].update(self.valid_zstd_elf_entries())
        if self.contract["gil_policy"] == "zero":
            report["sysconfig"]["Py_GIL_DISABLED"] = 0
        return report

    def zstd_manifest(self, identity, arch):
        documents = QUALIFIER["RELEASE_COMPONENTS"][
            "render_component_documents"
        ](self.release)
        component = "zstd/host-build" if identity == "host" else "zstd/%s-build" % arch
        prefix_identity = "host" if identity == "host" else identity
        machine = (
            "Advanced Micro Devices X86-64"
            if arch == "x86_64"
            else "AArch64"
        )
        return {
            "schema_version": 1,
            "kind": "crossforge-zstd-static-build",
            "version": "1.5.7",
            "identity": identity,
            "prefix": "/opt/crossforge/deps/zstd/1.5.7/%s" % prefix_identity,
            "compiler_dumpmachine": (
                "x86_64-redhat-linux" if identity == "host" else identity
            ),
            "flags": {
                "cflags": "-O2 -g0 -fPIC -fvisibility=hidden "
                "-ffile-prefix-map=/work/build/zstd=/usr/src/debug/crossforge-zstd",
                "cppflags": "-DZSTD_MULTITHREAD -DZSTD_NO_TRACE -DDEBUGLEVEL=0 "
                "-DZSTDLIB_VISIBLE=ZSTDLIB_HIDDEN "
                "-DZSTDERRORLIB_VISIBLE=ZSTDERRORLIB_HIDDEN "
                "-DZDICTLIB_VISIBLE=ZDICTLIB_HIDDEN "
                "-DZSTDLIB_STATIC_API=ZSTDLIB_HIDDEN "
                "-DZDICTLIB_STATIC_API=ZDICTLIB_HIDDEN",
                "pic_probe_ldflags": "-shared -Wl,-z,defs,-z,text "
                "-Wl,--whole-archive lib/libzstd.a "
                "-Wl,--no-whole-archive,--exclude-libs,libzstd.a -pthread",
            },
            "archive": {
                "path": "lib/libzstd.a",
                "sha256": "8" * 64,
                "members": [
                    {"name": "zstd_compress.o", "sha256": "9" * 64},
                    {"name": "zstd_decompress.o", "sha256": "a" * 64},
                ],
                "objects": 2,
            },
            "headers": {
                "zdict.h": "b" * 64,
                "zstd.h": "c" * 64,
                "zstd_errors.h": "d" * 64,
            },
            "pic_probe": {
                "sha256": "e" * 64,
                "machine": machine,
                "whole_archive": True,
                "no_zstd_exports": True,
                "no_dynamic_libzstd": True,
                "no_rpath": True,
            },
            "source_manifest_sha256": "f" * 64,
            "build_policy": {
                "component": "implementation/zstd-build-policy",
                "canonical_sha256": canonical_sha256(
                    documents["implementation/zstd-build-policy"]
                ),
            },
            "build_component": {
                "component": component,
                "canonical_sha256": canonical_sha256(documents[component]),
            },
            "policy": {
                "static_only": True,
                "position_independent": True,
                "multithread": True,
                "no_trace": True,
                "debug_level": 0,
                "visibility": "hidden",
                "legacy_support": 0,
                "exclude_archive_symbols": True,
            },
        }

    def valid_compile_zstd(self):
        if not self.contract["zstd"]:
            return {"policy": "absent", "module": None, "builds": None}
        host = self.zstd_manifest("host", "x86_64")
        target = self.zstd_manifest(TARGET, "x86_64")
        defined = list(QUALIFIER["ZSTD_REQUIRED_DEFINITIONS"])
        symbol_payload = {
            "required_definitions": defined,
            "defined": defined,
            "undefined": [],
            "dynamic_exports": [],
        }
        symbols = copy.deepcopy(symbol_payload)
        symbols["canonical_sha256"] = canonical_sha256(symbol_payload)
        module_path = (
            "lib/python%s/lib-dynload/_zstd.cpython-%s-x86_64-linux-gnu.so"
            % (self.minor, self.compact)
        )
        module_sha256 = next(
            value["sha256"]
            for name, value in self.valid_zstd_elf_entries().items()
            if name == module_path
        )
        return {
            "policy": "required",
            "version": "1.5.7",
            "module": {
                "path": module_path,
                "sha256": module_sha256,
                "needed": ["libc.so.6"],
                "symbols": symbols,
            },
            "builds": {
                "host": {
                    "manifest": host,
                    "manifest_sha256": serialized_sha256(host),
                },
                "target": {
                    "manifest": target,
                    "manifest_sha256": serialized_sha256(target),
                },
            },
        }

    def valid_zstd_elf_entries(self):
        if not self.contract["zstd"]:
            return {}
        path = (
            "lib/python%s/lib-dynload/_zstd.cpython-%s-x86_64-linux-gnu.so"
            % (self.minor, self.compact)
        )
        return {
            path: self.valid_elf_audit(path, "c" * 64)
        }

    def valid_overlay(self):
        packages_by_name = {
            provider["owner"]["name"]: copy.deepcopy(provider["owner"])
            for provider in self.abi_context["runtime_provider_evidence"][
                "providers"
            ]
        }
        packages = [packages_by_name[name] for name in sorted(packages_by_name)]
        identity = {
            "base_image": {
                "index_digest": self.release["base_image"]["digest"],
                "manifest_digest": self.release["base_image"]["manifests"]["amd64"],
            },
            "release_sha256": canonical_sha256(self.release),
            "target": {"arch": "x86_64", "triple": TARGET},
            "sysroot": {
                "lock_sha256": self.sysroot_sha256,
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
        loaded_objects = [
            runtime_root + provider["path"]
            for provider in self.abi_context["runtime_provider_evidence"][
                "providers"
            ]
        ]
        if self.contract["zstd"]:
            loaded_objects.append(
                "/opt/crossforge/python/%s/targets/%s/lib/python%s/lib-dynload/"
                "_zstd.cpython-%s-x86_64-linux-gnu.so"
                % (self.row, TARGET, self.minor, self.compact)
            )
        loaded_objects.sort()
        imports = copy.deepcopy(FINALIZER["REQUIRED_PROBE_IMPORTS"])
        if self.contract["zstd"]:
            imports.extend(["_zstd", "compression.zstd"])
        probe_zstd = zstd_evidence(self.contract["zstd"])
        return {
            "qualification_schema_version": 3,
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
                    self.sysroot_sha256
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
            "runtime_providers": copy.deepcopy(
                self.abi_context["runtime_provider_evidence"]
            ),
            "probe": {
                "schema_version": 2,
                "report_kind": "crossforge-cpython-probe",
                "mode": "core",
                "status": "passed",
                "target": TARGET,
                "version": self.version,
                "sysconfig": copy.deepcopy(probe_sysconfig),
                "imports": imports,
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
                    "algorithm": self.contract["hash_algorithm"],
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
                "zstd": copy.deepcopy(probe_zstd),
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
                "zstd": copy.deepcopy(probe_zstd),
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
            actual_elf_evidence=self.actual_elf_evidence(),
            abi_context_override=self.abi_context,
        )

    def validate_final_report(self, report):
        return FINALIZER["validate_final_report"](
            report,
            self.release,
            TARGET,
            self.version,
            self.abi_context,
            self.actual_elf_evidence(self.compile),
            True,
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
        self.assertEqual(report["qualification_schema_version"], 4)
        self.assertEqual(report["abi"], report["compile"]["abi"])
        self.assertEqual(
            report["qualification_components"],
            QUALIFIER["RELEASE_COMPONENTS"][
                "python_qualification_components"
            ](self.release),
        )
        self.assertEqual(
            report["qualification_components"],
            report["compile"]["qualification_components"],
        )
        self.assertEqual(report["python_sha256"], PYTHON_SHA256)
        self.assertEqual(report["extension_sha256"], EXTENSION_SHA256)
        self.assertEqual(
            set(report["executions"]), {"locked-sysroot", "clean-rocky"}
        )
        self.assertEqual(
            json.dumps(report, sort_keys=True),
            json.dumps(self.finalize(), sort_keys=True),
        )

    def test_actual_elf_collection_rejects_paths_outside_target_prefix(self):
        collector = FINALIZER["collect_actual_elf_evidence"]
        readelf = REPOSITORY / "scripts/finalize-cpython-qualification.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "target"
            root.mkdir()
            for name in ("../escape.so", "/tmp/escape.so"):
                with self.subTest(name=name), self.assertRaisesRegex(
                    FINALIZER["FinalizationError"], "unsafe"
                ):
                    collector(
                        {"elf_audit": {name: {}}},
                        self.abi_context,
                        root,
                        readelf,
                    )

            outside = Path(temporary) / "outside"
            outside.mkdir()
            (outside / "escape.so").write_bytes(b"not an ELF")
            (root / "lib").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                FINALIZER["FinalizationError"], "escapes the target prefix"
            ):
                collector(
                    {"elf_audit": {"lib/escape.so": {}}},
                    self.abi_context,
                    root,
                    readelf,
                )

    def test_finalizer_abi_inputs_are_bound_to_release_pins(self):
        self.assertEqual(
            FINALIZER["validate_release_abi_context"](
                self.release, self.abi_context
            ),
            FINALIZER["ABI_CONTRACT"]["release_abi_inputs"](
                self.release, "x86_64"
            ),
        )
        release = copy.deepcopy(self.release)
        release["abi"]["python"]["provider_catalogs"]["x86_64"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"],
            "ABI inputs differ from release.json",
        ):
            FINALIZER["validate_release_abi_context"](
                release, self.abi_context
            )

    def test_compile_qualification_component_identity_is_fail_closed(self):
        mutations = {
            "policy-digest": lambda value: value["policy"].update(
                canonical_sha256="0" * 64
            ),
            "aggregate-name": lambda value: value["aggregate"].update(
                component="implementation/python-qualification-policy"
            ),
            "swapped-roles": lambda value: value.update(
                policy=copy.deepcopy(value["aggregate"]),
                aggregate=copy.deepcopy(value["policy"]),
            ),
            "missing-role": lambda value: value.pop("policy"),
            "extra-role": lambda value: value.update(untrusted={}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.reset_fixture(VERSION)
                mutate(self.compile["qualification_components"])
                self.write_json(self.compile_path, self.compile)
                with self.assertRaisesRegex(
                    FINALIZER["FinalizationError"],
                    "qualification_components|component",
                ):
                    self.finalize()

    def test_final_qualification_components_are_revalidated(self):
        report = self.finalize()
        report["qualification_components"] = copy.deepcopy(
            report["qualification_components"]
        )
        report["qualification_components"]["aggregate"][
            "canonical_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"],
            "qualification_components|component",
        ):
            self.validate_final_report(report)

    def test_runtime_rejects_component_substitution_before_artifact_access(self):
        self.compile["qualification_components"]["policy"][
            "canonical_sha256"
        ] = "0" * 64
        self.write_json(self.compile_path, self.compile)
        runtime_root = self.directory / "runtime-root"
        runtime_root.mkdir()
        argv = [
            "run-cpython-runtime.py",
            "--compile-report",
            str(self.compile_path),
            "--release",
            str(self.release_path),
            "--runtime-root",
            str(runtime_root),
            "--runtime-provider-policy",
            "/src/config/python-runtime-providers.json",
            "--python-provider-catalog",
            "/work/config/python-provider-catalog.json",
            "--target-prefix",
            str(self.directory / "missing-prefix"),
            "--extension",
            str(self.directory / "missing-extension"),
            "--probe",
            str(self.directory / "missing-probe"),
            "--target",
            TARGET,
            "--version",
            self.version,
            "--tier",
            "locked-sysroot",
            "--output",
            str(self.directory / "runtime.json"),
        ]
        provider_target = {
            "owners": [
                provider["owner"]
                for provider in self.abi_context[
                    "runtime_provider_evidence"
                ]["providers"]
            ],
            "providers": self.abi_context["runtime_provider_evidence"][
                "providers"
            ],
        }
        provider_functions = {
            "load_json": lambda _path: {},
            "policy_target": lambda *_args: provider_target,
            "runtime_provider_evidence": lambda *_args, **_kwargs: copy.deepcopy(
                self.abi_context["runtime_provider_evidence"]
            ),
        }
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(
            RUNTIME_RUNNER["RUNTIME_PROVIDERS"], provider_functions
        ):
            with self.assertRaisesRegex(
                RUNTIME_RUNNER["RuntimeError_"], "qualification_components"
            ):
                RUNTIME_RUNNER["main"]()

    def test_runtime_provider_catalog_hashes_each_reviewed_dso(self):
        arch = self.abi_context["arch"]
        policy = RUNTIME_RUNNER["RUNTIME_PROVIDERS"]["load_json"](
            REPOSITORY / "config/python-runtime-providers.json"
        )
        catalog_path = REPOSITORY / (
            "evidence/abi/el8-%s-python-provider-catalog.json" % arch
        )
        reviewed = json.loads(catalog_path.read_text(encoding="utf-8"))
        summaries = self.compile["abi"]["provider_catalog"]["providers"]
        soname_by_path = {
            summary["path"]: summary["soname"] for summary in summaries
        }
        digest_by_soname = {
            summary["soname"]: summary["dso_sha256"]
            for summary in summaries
        }
        source_by_soname = {
            summary["soname"]: summary["source"]
            for summary in summaries
        }
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary)
            for soname in reviewed:
                (runtime_root / soname).write_bytes(b"provider")

            def fake_root_path(_root, logical):
                return runtime_root / soname_by_path[logical]

            def fake_record(_readelf, _path, identity, expected_soname=None):
                self.assertEqual(identity, expected_soname)
                return {}, copy.deepcopy(reviewed[identity])

            function = RUNTIME_RUNNER["validate_runtime_provider_catalog"]
            globals_ = function.__globals__
            with mock.patch.dict(
                globals_,
                {
                    "safe_root_path": fake_root_path,
                    "sha256_file": lambda path: digest_by_soname[
                        Path(path).name
                    ],
                },
            ), mock.patch.dict(
                RUNTIME_RUNNER["PYTHON_ABI"],
                {"elf_record_from_file": fake_record},
            ):
                result = function(
                    runtime_root,
                    self.compile,
                    policy,
                    catalog_path,
                    arch,
                    TARGET,
                    "locked-sysroot",
                    REPOSITORY / ("abi/el8/%s.json" % arch),
                    REPOSITORY
                    / ("evidence/abi/el8-%s-sysroot.json" % arch),
                )
                self.assertEqual(
                    result["canonical_sha256"],
                    self.abi_context["runtime_provider_evidence"][
                        "provider_catalog_sha256"
                    ],
                )

            def clean_runtime_sha256(path):
                soname = Path(path).name
                if source_by_soname[soname] == "python-runtime":
                    return digest_by_soname[soname]
                return "0" * 64

            with mock.patch.dict(
                globals_,
                {
                    "safe_root_path": fake_root_path,
                    "sha256_file": clean_runtime_sha256,
                },
            ), mock.patch.dict(
                RUNTIME_RUNNER["PYTHON_ABI"],
                {"elf_record_from_file": fake_record},
            ):
                function(
                    runtime_root,
                    self.compile,
                    policy,
                    catalog_path,
                    arch,
                    TARGET,
                    "clean-rocky",
                    REPOSITORY / ("abi/el8/%s.json" % arch),
                    REPOSITORY
                    / ("evidence/abi/el8-%s-sysroot.json" % arch),
                )

            with mock.patch.dict(
                globals_,
                {
                    "safe_root_path": fake_root_path,
                    "sha256_file": lambda _path: "0" * 64,
                },
            ):
                with self.assertRaisesRegex(
                    RUNTIME_RUNNER["RuntimeError_"],
                    "runtime provider bytes differ",
                ):
                    function(
                        runtime_root,
                        self.compile,
                        policy,
                        catalog_path,
                        arch,
                        TARGET,
                        "locked-sysroot",
                        REPOSITORY / ("abi/el8/%s.json" % arch),
                        REPOSITORY
                        / (
                            "evidence/abi/el8-%s-sysroot.json" % arch
                        ),
                    )

    def test_device_loader_must_use_the_reviewed_provider_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            prefix = Path(temporary) / "sdk"
            reviewed = root / "usr/lib64/libssl.so.1.1"
            alternate = root / "lib64/libssl.so.1.1"
            loader = root / "usr/lib64/ld-linux-x86-64.so.2"
            reviewed.parent.mkdir(parents=True)
            alternate.parent.mkdir(parents=True)
            prefix.mkdir()
            reviewed.write_bytes(b"reviewed")
            alternate.write_bytes(b"alternate")
            loader.write_bytes(b"loader")
            guest_loader = alternate.parent / "ld-linux-x86-64.so.2"
            guest_loader.symlink_to("../usr/lib64/ld-linux-x86-64.so.2")
            catalog = {
                "paths": {
                    "ld-linux-x86-64.so.2": loader,
                    "libssl.so.1.1": reviewed,
                },
                "required_external": ["libssl.so.1.1"],
            }
            validate = RUNTIME_RUNNER["validate_device_loaded_objects"]
            accepted = validate(
                "calling init: %s\n" % reviewed,
                root,
                prefix,
                catalog,
                "/lib64/ld-linux-x86-64.so.2",
            )
            self.assertEqual(accepted, [str(reviewed)])
            accepted = validate(
                "calling init: /lib64/ld-linux-x86-64.so.2\n"
                "calling init: %s\n" % reviewed,
                root,
                prefix,
                catalog,
                "/lib64/ld-linux-x86-64.so.2",
            )
            self.assertEqual(accepted, sorted((str(loader), str(reviewed))))
            guest_loader.unlink()
            guest_loader.write_bytes(b"unreviewed loader")
            with self.assertRaisesRegex(
                RUNTIME_RUNNER["RuntimeError_"],
                "unreviewed guest loader",
            ):
                validate(
                    "calling init: /lib64/ld-linux-x86-64.so.2\n"
                    "calling init: %s\n" % reviewed,
                    root,
                    prefix,
                    catalog,
                    "/lib64/ld-linux-x86-64.so.2",
                )
            with self.assertRaisesRegex(
                RUNTIME_RUNNER["RuntimeError_"],
                "unreviewed provider path",
            ):
                validate(
                    "calling init: %s\n" % alternate,
                    root,
                    prefix,
                    catalog,
                    "/lib64/ld-linux-x86-64.so.2",
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
                self.assertEqual(report["zstd"], report["compile"]["zstd"])
                self.assertEqual(
                    report["zstd"]["policy"],
                    "required" if contract["zstd"] else "absent",
                )
                if contract["zstd"]:
                    self.assertIn("_zstd", report["compile"]["required_modules"])
                else:
                    self.assertNotIn("_zstd", report["compile"]["required_modules"])
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
                    expected_zstd = zstd_evidence(contract["zstd"])
                    self.assertEqual(execution["probe"]["zstd"], expected_zstd)
                    self.assertEqual(
                        execution["device_probe"]["zstd"], expected_zstd
                    )
                    expected_imports = copy.deepcopy(
                        FINALIZER["REQUIRED_PROBE_IMPORTS"]
                    )
                    if contract["zstd"]:
                        expected_imports.extend(["_zstd", "compression.zstd"])
                    self.assertEqual(
                        execution["probe"]["imports"], expected_imports
                    )
                    self.assertEqual(
                        execution["probe"]["hash_algorithm"]["algorithm"],
                        contract["hash_algorithm"],
                    )
                    loaded_names = {
                        Path(item).name
                        for item in execution["device_loaded_objects"]
                    }
                    self.assertEqual(
                        any(name.startswith("_zstd.") for name in loaded_names),
                        contract["zstd"],
                    )

    def test_configure_arguments_follow_the_selected_adapter_api(self):
        validate = QUALIFIER["validate_configure_arguments"]
        build_triple = "x86_64-pc-linux-gnu"
        build_python = Path(
            "/opt/crossforge/python/cp310/build/bin/python3.10"
        )
        common = " ".join(
            (
                "--host=" + TARGET,
                "--build=" + build_triple,
                "--prefix=/opt/crossforge/python/cp310/targets/" + TARGET,
                "--with-computed-gotos=yes",
                "--with-ensurepip=no",
                "--disable-test-modules",
            )
        )
        legacy = ROW_CONTRACT["contract_for_version"]("3.10.21")
        self.assertEqual(
            validate(common, legacy, TARGET, build_triple, build_python), common
        )
        for unsupported in (
            "--with-build-python=" + str(build_python),
            "--with-pkg-config=yes",
        ):
            with self.subTest(unsupported=unsupported):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    validate(
                        common + " " + unsupported,
                        legacy,
                        TARGET,
                        build_triple,
                        build_python,
                    )

        cp39_build_python = Path(
            "/opt/crossforge/python/cp39/build/bin/python3.9"
        )
        cp39_required = (
            "--host=" + TARGET,
            "--build=" + build_triple,
            "--prefix=/opt/crossforge/python/cp39/targets/" + TARGET,
            "--with-computed-gotos=yes",
            "--with-ensurepip=no",
        )
        cp39_args = " ".join(cp39_required)
        cp39_legacy = ROW_CONTRACT["contract_for_version"]("3.9.25")
        self.assertEqual(
            validate(
                cp39_args,
                cp39_legacy,
                TARGET,
                build_triple,
                cp39_build_python,
            ),
            cp39_args,
        )
        cp39_invalid = [
            cp39_args.replace(option, option + "-wrong")
            for option in cp39_required
        ]
        cp39_invalid.extend(
            (
                cp39_args + " " + cp39_required[0],
                cp39_args + " --disable-test-modules",
                cp39_args + " --disable-test-modules=yes",
                cp39_args
                + " --with-build-python="
                + str(cp39_build_python),
                cp39_args + " --with-pkg-config=yes",
                cp39_args + " HOSTRUNNER=qemu",
                "'unterminated",
                None,
            )
        )
        for invalid in cp39_invalid:
            with self.subTest(cp39_invalid=invalid):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    validate(
                        invalid,
                        cp39_legacy,
                        TARGET,
                        build_triple,
                        cp39_build_python,
                    )

        modern = ROW_CONTRACT["contract_for_version"]("3.13.15")
        modern_build_python = Path(
            "/opt/crossforge/python/cp313/build/bin/python3.13"
        )
        modern_common = common.replace("/cp310/", "/cp313/")
        modern_args = modern_common + " --with-build-python=" + str(
            modern_build_python
        ) + " --with-pkg-config=yes"
        self.assertEqual(
            validate(
                modern_args,
                modern,
                TARGET,
                build_triple,
                modern_build_python,
            ),
            modern_args,
        )
        for invalid in (
            modern_common,
            modern_common + " --with-build-python=" + str(modern_build_python),
            modern_common + " --with-pkg-config=yes",
            modern_common + " HOSTRUNNER=qemu",
            modern_args + " --host=" + TARGET,
            modern_args.replace("--with-ensurepip=no", "--with-ensurepip=nope"),
            None,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    validate(
                        invalid,
                        modern,
                        TARGET,
                        build_triple,
                        modern_build_python,
                    )

        for malformed in (
            common.replace("--host=" + TARGET, "--host=" + TARGET + "-evil"),
            common.replace(
                "--build=" + build_triple,
                "--build=" + build_triple + "-junk",
            ),
            common.replace(
                "--with-computed-gotos=yes",
                "--with-computed-gotos=yes-no",
            ),
            common.replace("--with-ensurepip=no", "--with-ensurepip=nope"),
            common.replace(
                "--disable-test-modules",
                "--disable-test-modules-extra",
            ),
            common + " --host=" + TARGET,
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(QUALIFIER["QualificationError"]):
                    validate(
                        malformed,
                        legacy,
                        TARGET,
                        build_triple,
                        build_python,
                    )

    def test_required_zstd_evidence_imports_and_loaded_object_are_all_mandatory(self):
        mutations = (
            (
                "core-version",
                lambda report: report["probe"]["zstd"].__setitem__(
                    "version", "1.5.6"
                ),
            ),
            (
                "device-dictionary",
                lambda report: report["device_probe"]["zstd"][
                    "dictionary"
                ].__setitem__("finalized", False),
            ),
            (
                "boolean-type-confusion",
                lambda report: report["probe"]["zstd"][
                    "dictionary"
                ].__setitem__("trained", 1),
            ),
            (
                "required-import",
                lambda report: report["probe"]["imports"].remove(
                    "compression.zstd"
                ),
            ),
            (
                "extension-object",
                lambda report: report.__setitem__(
                    "device_loaded_objects",
                    [
                        item
                        for item in report["device_loaded_objects"]
                        if not Path(item).name.startswith("_zstd.")
                    ],
                ),
            ),
            (
                "forged-extension-object",
                lambda report: report.__setitem__(
                    "device_loaded_objects",
                    sorted(
                        (
                            item
                            if not Path(item).name.startswith("_zstd.")
                            else str(Path(item).with_name("_zstd.forged.so"))
                        )
                        for item in report["device_loaded_objects"]
                    ),
                ),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.reset_fixture(ZSTD_VERSION)
                mutate(self.locked)
                self.write_json(self.locked_path, self.locked)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()

    def test_compile_zstd_static_and_manifest_evidence_is_fail_closed(self):
        def dynamic_dependency(report):
            module = report["zstd"]["module"]
            module["needed"].append("libzstd.so.1")
            report["elf_audit"][module["path"]]["needed"].append("libzstd.so.1")

        def missing_static_definition(report):
            symbols = report["zstd"]["module"]["symbols"]
            symbols["defined"].remove("ZSTD_versionNumber")
            payload = {
                name: symbols[name]
                for name in (
                    "required_definitions",
                    "defined",
                    "undefined",
                    "dynamic_exports",
                )
            }
            symbols["canonical_sha256"] = canonical_sha256(payload)

        def exported_private_symbol(report):
            symbols = report["zstd"]["module"]["symbols"]
            symbols["dynamic_exports"] = ["ZSTD_versionNumber"]
            payload = {
                name: symbols[name]
                for name in (
                    "required_definitions",
                    "defined",
                    "undefined",
                    "dynamic_exports",
                )
            }
            symbols["canonical_sha256"] = canonical_sha256(payload)

        def forged_component(report):
            build = report["zstd"]["builds"]["target"]
            build["manifest"]["build_component"]["canonical_sha256"] = "0" * 64
            build["manifest_sha256"] = serialized_sha256(build["manifest"])

        def swapped_architecture(report):
            build = report["zstd"]["builds"]["target"]
            build["manifest"]["identity"] = "aarch64-unknown-linux-gnu"
            build["manifest"]["compiler_dumpmachine"] = "aarch64-unknown-linux-gnu"
            build["manifest"]["prefix"] = (
                "/opt/crossforge/deps/zstd/1.5.7/aarch64-unknown-linux-gnu"
            )
            build["manifest"]["pic_probe"]["machine"] = "AArch64"
            build["manifest_sha256"] = serialized_sha256(build["manifest"])

        def different_source(report):
            build = report["zstd"]["builds"]["target"]
            build["manifest"]["source_manifest_sha256"] = "0" * 64
            build["manifest_sha256"] = serialized_sha256(build["manifest"])

        for name, mutate in (
            ("dynamic", dynamic_dependency),
            ("missing-definition", missing_static_definition),
            ("export", exported_private_symbol),
            ("component", forged_component),
            ("architecture", swapped_architecture),
            ("source", different_source),
        ):
            with self.subTest(name=name):
                self.reset_fixture(ZSTD_VERSION)
                mutate(self.compile)
                self.write_json(self.compile_path, self.compile)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()

    def test_final_report_promotes_exact_compile_zstd_evidence(self):
        self.reset_fixture(ZSTD_VERSION)
        report = self.finalize()
        self.assertIs(
            FINALIZER["validate_qualification_zstd"](
                report, self.release, TARGET, ZSTD_VERSION
            ),
            report["zstd"],
        )
        report["zstd"] = copy.deepcopy(report["zstd"])
        report["zstd"]["module"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"], "zstd evidence mismatch"
        ):
            self.validate_final_report(report)

    def test_absent_compile_zstd_contract_rejects_module_inventory(self):
        path = (
            "lib/python3.13/lib-dynload/"
            "_zstd.cpython-313-x86_64-linux-gnu.so"
        )
        self.compile["required_modules"]["_zstd"] = path
        self.compile["elf_audit"][path] = self.valid_elf_audit(
            path, "0" * 64
        )
        self.write_json(self.compile_path, self.compile)
        with self.assertRaises(FINALIZER["FinalizationError"]):
            self.finalize()

    def test_qualifier_zstd_symbol_audit_uses_static_and_dynamic_tables(self):
        outputs = {
            "-d": " 0x1 (NEEDED) Shared library: [libc.so.6]\n",
            "--dyn-syms": "Symbol table '.dynsym' contains 1 entries:\n",
            "--defined-only": "\n".join(
                "%s t 0 1" % name
                for name in QUALIFIER["ZSTD_REQUIRED_DEFINITIONS"]
            ),
            "--undefined-only": "PyLong_FromLong U\n",
        }

        def fake_run(arguments, cwd=None, env=None):
            for option, output in outputs.items():
                if option in [str(item) for item in arguments]:
                    return output, ""
            raise AssertionError(arguments)

        function_globals = QUALIFIER["audit_zstd_module"].__globals__
        original = function_globals["run"]
        function_globals["run"] = fake_run
        try:
            result = QUALIFIER["audit_zstd_module"](
                Path("target-readelf"),
                Path("target-nm"),
                Path("_zstd.so"),
                {"needed": ["libc.so.6"], "sha256": "a" * 64},
            )
            self.assertEqual(
                result["symbols"]["defined"],
                list(QUALIFIER["ZSTD_REQUIRED_DEFINITIONS"]),
            )
            outputs["--undefined-only"] = "ZSTD_versionNumber U\n"
            with self.assertRaisesRegex(
                QUALIFIER["QualificationError"], "unresolved zstd"
            ):
                QUALIFIER["audit_zstd_module"](
                    Path("target-readelf"),
                    Path("target-nm"),
                    Path("_zstd.so"),
                    {"needed": ["libc.so.6"], "sha256": "a" * 64},
                )
        finally:
            function_globals["run"] = original

    def test_absent_zstd_policy_rejects_evidence_imports_and_loaded_objects(self):
        mutations = (
            (
                "available-type-confusion",
                lambda report: report["probe"]["zstd"].__setitem__(
                    "available", 0
                ),
            ),
            (
                "import",
                lambda report: report["probe"]["imports"].append(
                    "compression.zstd"
                ),
            ),
            (
                "extension-object",
                lambda report: report["device_loaded_objects"].append(
                    "/opt/crossforge/python/cp313/targets/%s/lib/python3.13/"
                    "lib-dynload/_zstd.cpython-313-x86_64-linux-gnu.so"
                    % TARGET
                ),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.reset_fixture(VERSION)
                mutate(self.locked)
                self.locked["device_loaded_objects"].sort()
                self.write_json(self.locked_path, self.locked)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()

    def test_dynamic_libzstd_is_rejected_from_every_runtime_evidence_channel(self):
        mutations = (
            (
                "interpreter-loader",
                "loader_dependencies",
                "libzstd.so.1 => /runtime-locked/usr/lib64/libzstd.so.1",
            ),
            (
                "device-loader",
                "device_loader_dependencies",
                "/runtime-locked/usr/lib64/libzstd.so.1",
            ),
            (
                "loaded-object",
                "device_loaded_objects",
                "/runtime-locked/usr/lib64/libzstd.so.1",
            ),
        )
        for name, field, value in mutations:
            with self.subTest(name=name):
                self.reset_fixture(ZSTD_VERSION)
                self.locked[field].append(value)
                self.locked[field].sort()
                self.write_json(self.locked_path, self.locked)
                with self.assertRaisesRegex(
                    FINALIZER["FinalizationError"], "dynamic libzstd"
                ):
                    self.finalize()

    def test_dynamic_libzstd_is_rejected_from_every_compile_elf(self):
        for version, select_path in (
            (VERSION, lambda report: "bin/python3.13"),
            (
                ZSTD_VERSION,
                lambda report: report["required_modules"]["_ssl"],
            ),
        ):
            with self.subTest(version=version):
                self.reset_fixture(version)
                path = select_path(self.compile)
                self.compile["elf_audit"][path]["needed"].append(
                    "libzstd.so.1"
                )
                self.compile["elf_audit"][path]["needed"].sort()
                self.write_json(self.compile_path, self.compile)
                with self.assertRaises(FINALIZER["FinalizationError"]):
                    self.finalize()

    def test_qualifier_applies_dynamic_zstd_policy_to_complete_inventory(self):
        audit = {
            "bin/python3.13": {"needed": ["libc.so.6"]},
            "lib/python3.13/lib-dynload/_ssl.so": {
                "needed": ["libcrypto.so.1.1"]
            },
        }
        self.assertIs(
            QUALIFIER["validate_global_zstd_linkage"](audit), audit
        )
        audit["lib/python3.13/lib-dynload/_ssl.so"]["needed"].append(
            "libzstd.so.1.5.7"
        )
        with self.assertRaisesRegex(
            QUALIFIER["QualificationError"],
            "dynamically depends on libzstd",
        ):
            QUALIFIER["validate_global_zstd_linkage"](audit)

    def test_runtime_runner_rejects_every_dynamic_libzstd_soname_form(self):
        reject = RUNTIME_RUNNER["reject_dynamic_zstd"]
        for evidence in (
            "libzstd.so => /runtime/usr/lib64/libzstd.so",
            "libzstd.so.1 => /runtime/usr/lib64/libzstd.so.1",
            "/runtime/usr/lib64/libzstd.so.1.5.7",
        ):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(
                    RUNTIME_RUNNER["RuntimeError_"], "dynamic libzstd"
                ):
                    reject([evidence], "test runtime")
        reject(
            [
                "/opt/crossforge/python/cp314/targets/%s/lib/python3.14/"
                "lib-dynload/_zstd.cpython-314-x86_64-linux-gnu.so" % TARGET
            ],
            "test runtime",
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

    def test_runtime_host_scripts_remain_python36_parseable(self):
        for name in (
            "python_zstd_evidence.py",
            "qualify-cpython.py",
            "finalize-cpython-qualification.py",
            "run-cpython-runtime.py",
        ):
            with self.subTest(name=name):
                ast.parse(
                    (REPOSITORY / "scripts" / name).read_text(encoding="utf-8"),
                    filename=name,
                    feature_version=(3, 6),
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

    def test_runtime_report_rejects_same_soname_from_another_path(self):
        provider = self.abi_context["runtime_provider_evidence"][
            "providers"
        ][0]
        expected = "/runtime-locked" + provider["path"]
        self.locked["device_loaded_objects"].remove(expected)
        self.locked["device_loaded_objects"].append(
            "/runtime-locked/lib64/" + provider["soname"]
        )
        self.locked["device_loaded_objects"].sort()
        self.write_json(self.locked_path, self.locked)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"],
            "unreviewed provider path",
        ):
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

    def test_python_is_the_only_dynamic_executable_in_the_elf_audit(self):
        module = self.compile["required_modules"]["_ssl"]
        self.compile["elf_audit"][module]["elf_policy"] = (
            self.valid_elf_policy(module, executable=True)
        )
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"],
            "shared-object classification mismatch",
        ):
            self.finalize()

    def test_python_extension_cannot_be_marked_pie(self):
        module = self.compile["required_modules"]["_ssl"]
        self.compile["elf_audit"][module]["elf_policy"]["properties"][
            "pie"
        ] = True
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"],
            "shared-object classification mismatch",
        ):
            self.finalize()

    def test_python_global_record_is_the_actual_python_elf_record(self):
        python_global = self.compile["abi"]["python_global"]
        python_global["record"]["unversioned_exports"].append(
            "forged_python_api"
        )
        python_global["record"]["default_exports"].append(
            "forged_python_api"
        )
        python_global["elf_record_sha256"] = canonical_sha256(
            python_global["record"]
        )
        python_global["default_export_count"] += 1
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(
            FINALIZER["FinalizationError"],
            "Python global record differs from the actual Python audit",
        ):
            self.finalize()

    def test_required_module_paths_are_unique_and_abi_exact(self):
        shared = next(iter(self.compile["required_modules"].values()))
        self.compile["required_modules"] = {
            name: shared for name in self.compile["required_modules"]
        }
        self.write_json(self.compile_path, self.compile)
        with self.assertRaisesRegex(FINALIZER["FinalizationError"], "module paths"):
            self.finalize()

    def test_elf_ownership_version_namespaces_and_allowlists_are_strict(self):
        invalid_imports = (
            {
                "provider": "libc.so.6",
                "name": "memcpy",
                "version": "GLIBC_999.0",
                "binding": "GLOBAL",
            },
            {
                "provider": "libc.so.6",
                "name": "memcpy",
                "version": "EVIL_1.0",
                "binding": "GLOBAL",
            },
            {
                "provider": "libc.so.6",
                "name": "memcpy",
                "version": [],
                "binding": "GLOBAL",
            },
        )
        for imported in invalid_imports:
            with self.subTest(imported=imported):
                report = copy.deepcopy(self.compile)
                report["elf_audit"]["bin/python3.13"]["ownership"][
                    "core_versioned"
                ] = [imported]
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
            self.validate_final_report(report)

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
        abi_context = FINALIZER["default_abi_context"](target)
        report = copy.deepcopy(self.locked)
        report["target"] = target
        report["runtime"]["identity_sha256"] = abi_context[
            "runtime_provider_evidence"
        ]["sysroot_lock_sha256"]
        report["runtime_providers"] = copy.deepcopy(
            abi_context["runtime_provider_evidence"]
        )
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
            "zstd": self.contract["zstd"],
            "hash_algorithm": self.contract["hash_algorithm"],
            "release_sha256": canonical_sha256(self.release),
            "sysroot_sha256": abi_context["runtime_provider_evidence"][
                "sysroot_lock_sha256"
            ],
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
            abi_context,
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
                abi_context,
            )


if __name__ == "__main__":
    unittest.main()

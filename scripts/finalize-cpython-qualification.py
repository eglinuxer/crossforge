#!/usr/bin/env python3
"""Bind static and dual-runtime CPython qualification evidence."""

import argparse
import hashlib
import json
import re
import runpy
import sys
from pathlib import Path


class FinalizationError(RuntimeError):
    pass


TARGET_AUDIT = runpy.run_path(
    str(Path(__file__).with_name("target_artifact_audit.py"))
)
TargetAuditError = TARGET_AUDIT["AuditError"]
EXEC_OPERATIONS = TARGET_AUDIT["EXEC_OPERATIONS"]
LOADER_OPERATIONS = TARGET_AUDIT["LOADER_OPERATIONS"]
ROW_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("python_row_contract.py"))
)
ContractError = ROW_CONTRACT["ContractError"]
RELEASE_COMPONENTS = runpy.run_path(
    str(Path(__file__).with_name("render-release-components.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]
ZSTD_EVIDENCE = runpy.run_path(
    str(Path(__file__).with_name("python_zstd_evidence.py"))
)
ZstdEvidenceError = ZSTD_EVIDENCE["ZstdEvidenceError"]


COMPILE_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "target",
    "version",
    "adapter",
    "release_sha256",
    "source",
    "sysroot_sha256",
    "sysroot_transaction_sha256",
    "target_prefix",
    "build_python",
    "target_artifact_guard",
    "python_sha256",
    "extension",
    "required_modules",
    "sysconfig",
    "sdk_tree",
    "elf_audit",
    "zstd",
}
COMPILE_SOURCE_KEYS = {
    "url",
    "size",
    "sha256",
    "sigstore_bundle_sha256",
    "sigstore_verification",
}
BUILD_PYTHON_KEYS = {"path", "version", "sha256", "sdk_tree"}
EXTENSION_KEYS = {"name", "sha256"}
SDK_TREE_KEYS = {"entries", "canonical_sha256"}
ELF_AUDIT_KEYS = {"needed", "required_versions", "sha256"}
TARGET_ARTIFACT_GUARD_KEYS = {
    "execution_canaries",
    "loader_canaries",
    "records",
    "denied_execution_attempts",
    "denied_loader_attempts",
    "canonical_sha256",
}

RUNTIME_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "target",
    "version",
    "adapter",
    "tier",
    "status",
    "release_sha256",
    "compile_report_sha256",
    "python_sha256",
    "extension_sha256",
    "probe_sha256",
    "runtime",
    "executor",
    "loader_dependencies",
    "device_loader_dependencies",
    "device_loaded_objects",
    "probe",
    "device_probe",
}
RUNTIME_IDENTITY_KEYS = {
    "kind",
    "identity_sha256",
    "os_release_sha256",
    "loader_sha256",
    "overlay_evidence",
}
FINAL_REPORT_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "status",
    "target",
    "version",
    "adapter",
    "release_sha256",
    "source",
    "sysroot_sha256",
    "python_sha256",
    "extension_sha256",
    "probe_sha256",
    "compile_report_sha256",
    "compile",
    "zstd",
    "runtime_result_sha256",
    "executions",
}
RUNTIME_TIERS = {"locked-sysroot", "clean-rocky"}
EXECUTOR_KEYS = {"kind", "binary_sha256", "version", "cpu", "uname_release"}
CORE_PROBE_KEYS = {
    "schema_version",
    "report_kind",
    "mode",
    "status",
    "target",
    "version",
    "sysconfig",
    "imports",
    "functionality",
    "extension",
    "hash_algorithm",
    "threading",
    "semaphore",
    "network",
    "timezone",
    "wchar",
    "zstd",
}
DEVICE_PROBE_KEYS = {
    "schema_version",
    "report_kind",
    "mode",
    "status",
    "target",
    "version",
    "sysconfig",
    "probe",
    "zstd",
}
PROBE_SYSCONFIG_KEYS = {
    "arch",
    "build_gnu_type",
    "cache_tag",
    "cc",
    "ext_suffix",
    "host_gnu_type",
    "multiarch",
    "platform",
    "prefix",
    "soabi",
}
CORE_OBJECT_KEYS = {
    "functionality": {
        "compression_roundtrips",
        "ctypes_strlen",
        "hashlib_sha256",
        "openssl",
        "sqlite",
        "uuid5",
    },
    "extension": {"answer", "file", "module"},
    "hash_algorithm": {"algorithm", "hash_bits", "seed_bits"},
    "threading": {"event", "result"},
    "semaphore": {
        "multiprocessing_lock",
        "unnamed_acquire_release",
        "unnamed_get_value",
    },
    "network": {"address", "family", "port"},
    "timezone": {"posix_rule", "tzset", "utc_epoch"},
    "wchar": {"code_points", "cpython_api", "wchar_bytes"},
}
PTY_KEYS = {"character_devices", "isatty", "roundtrip_sha256"}
ZSTD_ABSENT_KEYS = {"available", "policy", "rejected_imports"}
ZSTD_REQUIRED_KEYS = {
    "available",
    "corrupt_error",
    "dictionary",
    "multithread",
    "payload_sha256",
    "policy",
    "roundtrips",
    "version",
    "version_info",
}

TARGETS = {
    "x86_64-unknown-linux-gnu": "amd64",
    "aarch64-unknown-linux-gnu": "arm64",
}
REQUIRED_MODULES = {
    "_bz2",
    "_ctypes",
    "_hashlib",
    "_lzma",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "zlib",
}
RUNTIME_PACKAGE_NAMES = (
    "bzip2-libs",
    "libffi",
    "libuuid",
    "openssl-libs",
    "sqlite-libs",
    "xz-libs",
    "zlib",
)
REQUIRED_PROBE_IMPORTS = [
    "_bz2",
    "_ctypes",
    "_hashlib",
    "_lzma",
    "_multiprocessing",
    "_sqlite3",
    "_ssl",
    "_uuid",
    "bz2",
    "ctypes",
    "fcntl",
    "hashlib",
    "lzma",
    "pty",
    "select",
    "sqlite3",
    "ssl",
    "termios",
    "tty",
    "uuid",
    "zlib",
]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ZSTD_FAMILY = ZSTD_EVIDENCE["FAMILY"]
ZSTD_REQUIRED_DEFINITIONS = ZSTD_EVIDENCE["REQUIRED_DEFINITIONS"]


def require(condition, message):
    if not condition:
        raise FinalizationError(message)


def require_exact_abi_map(actual, expected, path):
    require_exact_keys(actual, set(expected), path)
    for name, expected_value in expected.items():
        actual_value = actual[name]
        if type(expected_value) is int:
            require(
                type(actual_value) is int and actual_value == expected_value,
                "%s %s mismatch" % (path, name),
            )
        else:
            require(
                actual_value == expected_value,
                "%s %s mismatch" % (path, name),
            )


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalizationError("%s: %s" % (path, error)) from error
    require(isinstance(document, dict), "%s: root must be an object" % path)
    return document


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def serialized_sha256(value):
    payload = (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise FinalizationError("%s: %s" % (path, error)) from error
    return digest.hexdigest()


def require_exact_keys(value, keys, path):
    require(isinstance(value, dict), "%s must be an object" % path)
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    require(not missing, "%s is missing field(s): %s" % (path, ", ".join(missing)))
    require(not unknown, "%s has unknown field(s): %s" % (path, ", ".join(unknown)))


def require_string(value, path):
    require(isinstance(value, str) and value, "%s must be a non-empty string" % path)
    return value


def require_sha256(value, path):
    require_string(value, path)
    require(SHA256.fullmatch(value) is not None, "%s must be a lowercase SHA256" % path)
    return value


def version_tuple(value):
    return tuple(int(part) for part in value.split("."))


def release_context(release, target, version):
    require(target in TARGETS, "unsupported CPython target: %s" % target)

    try:
        targets = [item for item in release["targets"] if item["triple"] == target]
    except (KeyError, TypeError) as error:
        raise FinalizationError("release.json has an invalid qualification contract") from error
    require(len(targets) == 1, "release.json must select exactly one target")
    try:
        binding = ROW_CONTRACT["bind_release"](release, version=version)
    except ContractError as error:
        raise FinalizationError(str(error)) from error
    contract = binding["contract"]
    source = binding["entry"]["source"]
    sysroot = targets[0].get("sysroot")
    require(isinstance(sysroot, dict), "release target sysroot must be an object")
    require(sysroot.get("status") == "locked", "release target sysroot is not locked")
    return {
        "adapter": contract["adapter"],
        "gil_policy": contract["gil_policy"],
        "zstd": contract["zstd"],
        "hash_algorithm": contract["hash_algorithm"],
        "release_sha256": canonical_sha256(release),
        "source": source,
        "sysroot_sha256": require_sha256(
            sysroot.get("canonical_sha256"),
            "release target sysroot canonical_sha256",
        ),
    }


def expected_zstd_components(release, target_arch):
    try:
        return ZSTD_EVIDENCE["expected_components"](
            release,
            target_arch,
            RELEASE_COMPONENTS["render_component_documents"],
        )
    except ZstdEvidenceError as error:
        raise FinalizationError(str(error)) from error


def validate_global_zstd_linkage(elf_audit):
    try:
        return ZSTD_EVIDENCE["validate_no_dynamic_libzstd"](
            elf_audit, "compile report elf_audit"
        )
    except ZstdEvidenceError as error:
        raise FinalizationError(str(error)) from error


def validate_zstd_build_evidence(
    value,
    identity,
    prefix,
    machine,
    component_identity,
    policy_identity,
    path,
):
    try:
        return ZSTD_EVIDENCE["validate_build_evidence"](
            value,
            identity,
            prefix,
            machine,
            component_identity,
            policy_identity,
            path,
        )
    except ZstdEvidenceError as error:
        raise FinalizationError(str(error)) from error


def validate_compile_zstd_evidence(value, context, report, target, version):
    path = "compile report zstd"
    if not context["zstd"]:
        require_exact_keys(value, {"policy", "module", "builds"}, path)
        require(
            value == {"policy": "absent", "module": None, "builds": None},
            "%s absent policy mismatch" % path,
        )
        dynamic_prefix = "lib/python%s/lib-dynload/" % ".".join(version.split(".")[:2])
        require(
            not any(
                name.startswith(dynamic_prefix)
                and Path(name).name.startswith("_zstd.")
                for name in report["elf_audit"]
            ),
            "%s absent policy has an audited _zstd module" % path,
        )
        return value

    require_exact_keys(value, {"policy", "version", "module", "builds"}, path)
    require(
        value["policy"] == "required" and value["version"] == "1.5.7",
        "%s required policy/version mismatch" % path,
    )
    module = value["module"]
    require_exact_keys(module, {"path", "sha256", "needed", "symbols"}, path + " module")
    expected_path = report["required_modules"].get("_zstd")
    require(
        module["path"] == expected_path and expected_path in report["elf_audit"],
        "%s module path mismatch" % path,
    )
    require_sha256(module["sha256"], path + " module sha256")
    audit = report["elf_audit"][expected_path]
    require(
        module["sha256"] == audit["sha256"]
        and module["needed"] == audit["needed"],
        "%s module ELF binding mismatch" % path,
    )
    require(
        not any(item.startswith("libzstd.so") for item in module["needed"]),
        "%s module dynamically depends on libzstd" % path,
    )
    symbols = module["symbols"]
    symbol_keys = {
        "required_definitions",
        "defined",
        "undefined",
        "dynamic_exports",
        "canonical_sha256",
    }
    require_exact_keys(symbols, symbol_keys, path + " module symbols")
    require(
        symbols["required_definitions"] == ZSTD_REQUIRED_DEFINITIONS,
        "%s required static definitions mismatch" % path,
    )
    for name in ("defined", "undefined", "dynamic_exports"):
        observed = symbols[name]
        require(
            isinstance(observed, list)
            and observed == sorted(set(observed))
            and all(
                isinstance(item, str) and ZSTD_FAMILY.match(item)
                for item in observed
            ),
            "%s symbol list %s is not canonical" % (path, name),
        )
    require(
        not symbols["undefined"]
        and not symbols["dynamic_exports"]
        and set(ZSTD_REQUIRED_DEFINITIONS).issubset(symbols["defined"]),
        "%s static linkage symbol proof failed" % path,
    )
    symbol_payload = {
        name: symbols[name]
        for name in (
            "required_definitions",
            "defined",
            "undefined",
            "dynamic_exports",
        )
    }
    require_sha256(symbols["canonical_sha256"], path + " symbol digest")
    require(
        symbols["canonical_sha256"] == canonical_sha256(symbol_payload),
        "%s symbol evidence digest mismatch" % path,
    )

    require_exact_keys(value["builds"], {"host", "target"}, path + " builds")
    target_arch = "x86_64" if target.startswith("x86_64-") else "aarch64"
    machine = (
        "Advanced Micro Devices X86-64" if target_arch == "x86_64" else "AArch64"
    )
    components = expected_zstd_components(context["release"], target_arch)
    host = validate_zstd_build_evidence(
        value["builds"]["host"],
        "host",
        "/opt/crossforge/deps/zstd/1.5.7/host",
        "Advanced Micro Devices X86-64",
        components["host"],
        components["policy"],
        path + " host build",
    )
    target_build = validate_zstd_build_evidence(
        value["builds"]["target"],
        target,
        "/opt/crossforge/deps/zstd/1.5.7/%s" % target,
        machine,
        components["target"],
        components["policy"],
        path + " target build",
    )
    require(
        host["manifest"]["source_manifest_sha256"]
        == target_build["manifest"]["source_manifest_sha256"],
        "%s host/target source manifest mismatch" % path,
    )
    return value


def validate_dynamic_map(value, path):
    require(isinstance(value, dict), "%s must be an object" % path)
    for key in value:
        require_string(key, "%s key" % path)


def validate_overlay_evidence(value, context, compile_report, target, path):
    require_exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "qualification_only",
            "identity",
            "identity_sha256",
            "runtime_inventory",
        },
        path,
    )
    require(value["schema_version"] == 1, "%s schema mismatch" % path)
    require(
        value["kind"] == "crossforge-python-runtime-overlay"
        and value["qualification_only"] is True,
        "%s kind mismatch" % path,
    )
    identity = value["identity"]
    require_exact_keys(
        identity,
        {
            "base_image",
            "release_sha256",
            "target",
            "sysroot",
            "selected_packages",
            "selected_packages_sha256",
        },
        path + " identity",
    )
    require_exact_keys(
        identity["base_image"],
        {"index_digest", "manifest_digest"},
        path + " base_image",
    )
    expected_arch = TARGETS[target]
    release = context["release"]
    require(
        identity["base_image"]
        == {
            "index_digest": release["base_image"]["digest"],
            "manifest_digest": release["base_image"]["manifests"][expected_arch],
        },
        "%s base image mismatch" % path,
    )
    require(
        identity["release_sha256"] == context["release_sha256"],
        "%s release digest mismatch" % path,
    )
    require_exact_keys(identity["target"], {"arch", "triple"}, path + " target")
    target_arch = "x86_64" if expected_arch == "amd64" else "aarch64"
    require(
        identity["target"] == {"arch": target_arch, "triple": target},
        "%s target mismatch" % path,
    )
    require_exact_keys(
        identity["sysroot"],
        {"lock_sha256", "transaction_sha256"},
        path + " sysroot",
    )
    require(
        identity["sysroot"]
        == {
            "lock_sha256": context["sysroot_sha256"],
            "transaction_sha256": compile_report[
                "sysroot_transaction_sha256"
            ],
        },
        "%s sysroot contract mismatch" % path,
    )
    packages = identity["selected_packages"]
    require(
        isinstance(packages, list) and len(packages) == len(RUNTIME_PACKAGE_NAMES),
        "%s package count mismatch" % path,
    )
    for index, package in enumerate(packages):
        require_exact_keys(
            package,
            {"name", "nevra", "received_sha256"},
            "%s package %d" % (path, index),
        )
        require_string(package["name"], "%s package name" % path)
        require_string(package["nevra"], "%s package NEVRA" % path)
        require_sha256(package["received_sha256"], "%s package digest" % path)
    require(
        [package["name"] for package in packages] == list(RUNTIME_PACKAGE_NAMES),
        "%s package names/order mismatch" % path,
    )
    require(
        identity["selected_packages_sha256"] == canonical_sha256(packages),
        "%s package-set digest mismatch" % path,
    )
    require(
        value["identity_sha256"] == canonical_sha256(identity),
        "%s identity digest mismatch" % path,
    )
    inventory = value["runtime_inventory"]
    require_exact_keys(
        inventory,
        {
            "before_sha256",
            "before_item_count",
            "after_sha256",
            "after_item_count",
            "installed_nevras",
            "os_release_sha256",
        },
        path + " inventory",
    )
    for name in ("before_sha256", "after_sha256", "os_release_sha256"):
        require_sha256(inventory[name], "%s inventory %s" % (path, name))
    for name in ("before_item_count", "after_item_count"):
        require(
            type(inventory[name]) is int and inventory[name] > 0,
            "%s inventory %s is invalid" % (path, name),
        )
    require(
        inventory["installed_nevras"]
        == sorted(package["nevra"] for package in packages),
        "%s installed NEVRAs mismatch" % path,
    )
    return value


def validate_compile_report(report, context, target, version):
    require_exact_keys(report, COMPILE_KEYS, "compile report")
    require(
        report["qualification_schema_version"] == 2,
        "compile report schema version mismatch",
    )
    require(
        report["report_kind"] == "crossforge-cpython-compile",
        "compile report kind mismatch",
    )
    require(report["target"] == target, "compile report target mismatch")
    require(report["version"] == version, "compile report version mismatch")
    require(report["adapter"] == context["adapter"], "compile report adapter mismatch")
    require_sha256(report["release_sha256"], "compile report release_sha256")
    require(
        report["release_sha256"] == context["release_sha256"],
        "compile report release digest mismatch",
    )
    require_sha256(report["sysroot_sha256"], "compile report sysroot_sha256")
    require(
        report["sysroot_sha256"] == context["sysroot_sha256"],
        "compile report sysroot digest mismatch",
    )
    require_sha256(
        report["sysroot_transaction_sha256"],
        "compile report sysroot_transaction_sha256",
    )

    source = report["source"]
    require_exact_keys(source, COMPILE_SOURCE_KEYS, "compile report source")
    expected_source = context["source"]
    require_string(source["url"], "compile report source url")
    require(
        isinstance(source["size"], int)
        and not isinstance(source["size"], bool)
        and source["size"] > 0,
        "compile report source size must be a positive integer",
    )
    require_sha256(source["sha256"], "compile report source sha256")
    require_sha256(
        source["sigstore_bundle_sha256"],
        "compile report source sigstore_bundle_sha256",
    )
    try:
        expected_source_contract = {
            "url": expected_source["url"],
            "size": expected_source["size"],
            "sha256": expected_source["sha256"],
            "sigstore_bundle_sha256": expected_source["sigstore"]["bundle_sha256"],
            "sigstore_verification": expected_source["sigstore"]["verification"],
        }
    except (KeyError, TypeError) as error:
        raise FinalizationError("release CPython source is incomplete") from error
    require(source == expected_source_contract, "compile report source differs from release")

    minor = ".".join(version.split(".")[:2])
    compact_minor = minor.replace(".", "")
    expected_build_prefix = "/opt/crossforge/python/cp%s/build" % compact_minor
    expected_build_python = "%s/bin/python%s" % (expected_build_prefix, minor)
    build_python = report["build_python"]
    require_exact_keys(build_python, BUILD_PYTHON_KEYS, "compile report build_python")
    require(
        build_python["path"] == expected_build_python,
        "compile report build Python path mismatch",
    )
    require(
        build_python["version"] == version,
        "compile report build Python version mismatch",
    )
    require_sha256(build_python["sha256"], "compile report build_python sha256")
    require_exact_keys(
        build_python["sdk_tree"],
        SDK_TREE_KEYS,
        "compile report build_python sdk_tree",
    )
    require(
        type(build_python["sdk_tree"]["entries"]) is int
        and build_python["sdk_tree"]["entries"] > 0,
        "compile report build Python tree entry count is invalid",
    )
    require_sha256(
        build_python["sdk_tree"]["canonical_sha256"],
        "compile report build Python tree canonical_sha256",
    )

    guard = report["target_artifact_guard"]
    require_exact_keys(
        guard,
        TARGET_ARTIFACT_GUARD_KEYS,
        "compile report target_artifact_guard",
    )
    target_arch = "x86_64" if target.startswith("x86_64-") else "aarch64"
    build_directory = Path(
        "/work/build/cpython-cp%s-%s" % (compact_minor, target_arch)
    )
    prefix = Path(
        "/opt/crossforge/python/cp%s/targets/%s" % (compact_minor, target)
    )
    try:
        audit_summary = TARGET_AUDIT["validate_records"](
            guard["records"], build_directory, prefix
        )
    except TargetAuditError as error:
        raise FinalizationError(str(error)) from error
    require(
        guard["execution_canaries"] == list(EXEC_OPERATIONS)
        and guard["loader_canaries"] == list(LOADER_OPERATIONS)
        and guard["denied_execution_attempts"]
        == audit_summary["denied_execution_attempts"]
        and guard["denied_loader_attempts"]
        == audit_summary["denied_loader_attempts"],
        "compile report target-artifact guard summary mismatch",
    )
    require_sha256(
        guard["canonical_sha256"],
        "compile report target_artifact_guard canonical_sha256",
    )
    require(
        guard["canonical_sha256"]
        == TARGET_AUDIT["canonical_sha256"](audit_summary["records"]),
        "compile report target-artifact guard digest mismatch",
    )

    require_string(report["target_prefix"], "compile report target_prefix")
    expected_prefix = "/opt/crossforge/python/cp%s/targets/%s" % (
        compact_minor,
        target,
    )
    require(
        report["target_prefix"] == expected_prefix,
        "compile report target prefix mismatch",
    )
    require_sha256(report["python_sha256"], "compile report python_sha256")
    extension = report["extension"]
    require_exact_keys(extension, EXTENSION_KEYS, "compile report extension")
    require_string(extension["name"], "compile report extension name")
    require("/" not in extension["name"], "compile report extension name is not a basename")
    multiarch = target_arch + "-linux-gnu"
    expected_soabi = "cpython-%s-%s" % (compact_minor, multiarch)
    require(
        extension["name"] == "_crossforge.%s.so" % expected_soabi,
        "compile report extension name differs from the selected ABI",
    )
    require_sha256(extension["sha256"], "compile report extension sha256")

    modules = report["required_modules"]
    expected_required_modules = set(REQUIRED_MODULES)
    if context["zstd"]:
        expected_required_modules.add("_zstd")
    require_exact_keys(
        modules,
        expected_required_modules,
        "compile report required_modules",
    )
    expected_modules = {
        module: "lib/python%s/lib-dynload/%s.%s.so"
        % (minor, module, expected_soabi)
        for module in expected_required_modules
    }
    require(
        modules == expected_modules,
        "compile report required module paths differ from the selected ABI",
    )
    require(
        len(set(modules.values())) == len(expected_required_modules),
        "compile report required module paths are not unique",
    )
    expected_sysconfig = {
        "EXT_SUFFIX": ".%s.so" % expected_soabi,
        "HAVE_ALIGNED_REQUIRED": 0,
        "HAVE_USABLE_WCHAR_T": 0 if target_arch == "x86_64" else 1,
        "HOST_GNU_TYPE": target,
        "MULTIARCH": multiarch,
        "Py_DEBUG": 0,
        "SIZEOF_WCHAR_T": 4,
        "SOABI": expected_soabi,
    }
    if context["gil_policy"] == "zero":
        expected_sysconfig["Py_GIL_DISABLED"] = 0
    elif context["gil_policy"] != "absent":
        raise FinalizationError("unsupported CPython GIL policy")
    require_exact_abi_map(
        report["sysconfig"], expected_sysconfig, "compile report sysconfig"
    )
    require_exact_keys(report["sdk_tree"], SDK_TREE_KEYS, "compile report sdk_tree")
    require(
        isinstance(report["sdk_tree"]["entries"], int)
        and not isinstance(report["sdk_tree"]["entries"], bool)
        and report["sdk_tree"]["entries"] > 0,
        "compile report sdk_tree entries must be a positive integer",
    )
    require_sha256(
        report["sdk_tree"]["canonical_sha256"],
        "compile report sdk_tree canonical_sha256",
    )
    validate_dynamic_map(report["elf_audit"], "compile report elf_audit")
    require(report["elf_audit"], "compile report elf_audit must not be empty")
    python_audit_path = "bin/python%s" % minor
    extension_audit_path = "qualification/" + extension["name"]
    dynamic_prefix = "lib/python%s/lib-dynload/" % minor
    require(
        python_audit_path in report["elf_audit"]
        and extension_audit_path in report["elf_audit"],
        "compile report is missing the selected Python or extension audit",
    )
    for name, audit in report["elf_audit"].items():
        require(
            name in (python_audit_path, extension_audit_path)
            or (
                name.startswith(dynamic_prefix)
                and "/" not in name[len(dynamic_prefix):]
                and name.endswith(".%s.so" % expected_soabi)
            ),
            "compile report ELF audit path is outside the selected SDK: %s" % name,
        )
        require_exact_keys(audit, ELF_AUDIT_KEYS, "compile report elf_audit %s" % name)
        require(
            isinstance(audit["needed"], list)
            and all(isinstance(item, str) and item for item in audit["needed"]),
            "compile report ELF needed list is invalid: %s" % name,
        )
        require(
            audit["needed"] == sorted(set(audit["needed"])),
            "compile report ELF needed list is not canonical: %s" % name,
        )
        require(
            all("/" not in item for item in audit["needed"]),
            "compile report ELF has a path-qualified dependency: %s" % name,
        )
        versions = audit["required_versions"]
        require(
            isinstance(versions, dict)
            and set(versions).issubset({"GLIBC", "GCC"}),
            "compile report ELF version namespace is invalid: %s" % name,
        )
        ceilings = {"GLIBC": (2, 28), "GCC": (7, 0, 0)}
        for namespace, observed in versions.items():
            require(
                isinstance(observed, str)
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", observed)
                and version_tuple(observed) <= ceilings[namespace],
                "compile report ELF version exceeds policy: %s %s_%r"
                % (name, namespace, observed),
            )
        require_sha256(audit["sha256"], "compile report elf_audit %s sha256" % name)
    validate_global_zstd_linkage(report["elf_audit"])
    require(
        set(modules.values()).issubset(report["elf_audit"]),
        "compile report required modules are not all present in the ELF audit",
    )
    require(
        report["elf_audit"][python_audit_path]["sha256"]
        == report["python_sha256"],
        "compile report Python audit hash mismatch",
    )
    require(
        report["elf_audit"][extension_audit_path]["sha256"]
        == extension["sha256"],
        "compile report extension audit hash mismatch",
    )
    validate_compile_zstd_evidence(
        report["zstd"], context, report, target, version
    )
    return report


def validate_zstd_evidence(value, required, path):
    require(isinstance(value, dict), "%s must be an object" % path)
    if not required:
        require_exact_keys(value, ZSTD_ABSENT_KEYS, path)
        require(
            value["available"] is False
            and value["policy"] == "absent"
            and value["rejected_imports"]
            == ["_zstd", "compression.zstd"],
            "%s absent-policy evidence mismatch" % path,
        )
        return
    require_exact_keys(value, ZSTD_REQUIRED_KEYS, path)
    require_exact_keys(value["dictionary"], {"finalized", "trained"}, path + " dictionary")
    require_exact_keys(
        value["multithread"],
        {"nb_workers", "supported"},
        path + " multithread",
    )
    require(
        value["available"] is True
        and value["policy"] == "required"
        and value["version"] == "1.5.7"
        and value["version_info"] == [1, 5, 7]
        and all(type(item) is int for item in value["version_info"])
        and value["payload_sha256"]
        == "dd1fc53b1dfcac3378b57b9b8b2723c16f2b6aad628c940b09f6904fba3957a2"
        and value["roundtrips"]
        == [
            "dictionary",
            "multithread",
            "one-shot",
            "streaming",
            "tarfile",
            "zipfile",
        ]
        and value["dictionary"]["finalized"] is True
        and value["dictionary"]["trained"] is True
        and value["multithread"]["nb_workers"] == 1
        and type(value["multithread"]["nb_workers"]) is int
        and value["multithread"]["supported"] is True
        and value["corrupt_error"] == "ZstdError",
        "%s required-policy evidence mismatch" % path,
    )


def validate_probe(
    value,
    path,
    target,
    version,
    expected_mode,
    zstd_required,
    expected_hash_algorithm,
):
    expected_keys = CORE_PROBE_KEYS if expected_mode == "core" else DEVICE_PROBE_KEYS
    require_exact_keys(value, expected_keys, path)
    require(value["schema_version"] == 2, "%s schema mismatch" % path)
    require(value["report_kind"] == "crossforge-cpython-probe", "%s kind mismatch" % path)
    require(value["mode"] == expected_mode, "%s mode mismatch" % path)
    require(value["status"] == "passed", "%s did not pass" % path)
    require(value["target"] == target, "%s target mismatch" % path)
    require(value["version"] == version, "%s version mismatch" % path)
    validate_zstd_evidence(value["zstd"], zstd_required, "%s zstd" % path)

    sysconfig = value["sysconfig"]
    require_exact_keys(sysconfig, PROBE_SYSCONFIG_KEYS, "%s sysconfig" % path)
    for name, observed in sysconfig.items():
        require_string(observed, "%s sysconfig %s" % (path, name))
    require(
        sysconfig["host_gnu_type"] == target,
        "%s sysconfig target mismatch" % path,
    )
    target_arch = "x86_64" if target.startswith("x86_64-") else "aarch64"
    multiarch = target_arch + "-linux-gnu"
    compact_minor = "".join(version.split(".")[:2])
    expected_prefix = "/opt/crossforge/python/cp%s/targets/%s" % (
        compact_minor,
        target,
    )
    require(
        sysconfig["arch"] == target_arch
        and sysconfig["build_gnu_type"] == "x86_64-pc-linux-gnu"
        and sysconfig["cache_tag"] == "cpython-%s" % compact_minor
        and sysconfig["cc"]
        == "/opt/crossforge/targets/%s/bin/%s-gcc --sysroot=/opt/crossforge/sysroots/el8/%s"
        % (target, target, target_arch)
        and sysconfig["ext_suffix"]
        == ".cpython-%s-%s.so" % (compact_minor, multiarch)
        and sysconfig["multiarch"] == multiarch
        and sysconfig["platform"] == "linux-%s" % target_arch
        and sysconfig["soabi"] == "cpython-%s-%s" % (compact_minor, multiarch)
        and sysconfig["prefix"] == expected_prefix,
        "%s sysconfig ABI identity mismatch" % path,
    )

    if expected_mode == "core":
        imports = value["imports"]
        require(
            isinstance(imports, list)
            and imports
            and all(isinstance(item, str) and item for item in imports),
            "%s imports must be a non-empty string array" % path,
        )
        require(
            imports
            == REQUIRED_PROBE_IMPORTS
            + (["_zstd", "compression.zstd"] if zstd_required else []),
            "%s imports differ from the required module set" % path,
        )
        for name, keys in CORE_OBJECT_KEYS.items():
            require_exact_keys(value[name], keys, "%s %s" % (path, name))
        require(
            value["functionality"]["compression_roundtrips"]
            == ["bz2", "lzma", "zlib"]
            and value["functionality"]["ctypes_strlen"] == 10
            and value["functionality"]["hashlib_sha256"]
            == "822da7168e47d27301f5c747b5e678f593d60dc700049d33d3d3e1381dac1630"
            and value["functionality"]["uuid5"]
            == "d2222479-a666-5841-bee6-944f95190b64"
            and value["functionality"]["sqlite"] == "3.26.0"
            and isinstance(value["functionality"]["openssl"], str)
            and value["functionality"]["openssl"].startswith("OpenSSL 1.1.1"),
            "%s library functionality evidence mismatch" % path,
        )
        require(
            value["extension"]["answer"] == 42
            and value["extension"]["module"] == "_crossforge"
            and value["extension"]["file"]
            == "_crossforge.cpython-%s-%s.so" % (compact_minor, multiarch),
            "%s extension evidence mismatch" % path,
        )
        require(
            value["hash_algorithm"]
            == {
                "algorithm": expected_hash_algorithm,
                "hash_bits": 64,
                "seed_bits": 128,
            },
            "%s hash algorithm evidence mismatch" % path,
        )
        require(
            value["threading"] == {"event": True, "result": 5050}
            and value["semaphore"]
            == {
                "multiprocessing_lock": True,
                "unnamed_acquire_release": True,
                "unnamed_get_value": True,
            },
            "%s threading/semaphore evidence mismatch" % path,
        )
        require(
            value["network"]
            == {"address": "127.0.0.1", "family": "AF_INET", "port": 443}
            and value["timezone"]
            == {"posix_rule": True, "tzset": True, "utc_epoch": True}
            and value["wchar"]
            == {"code_points": 17, "cpython_api": True, "wchar_bytes": 4},
            "%s platform capability evidence mismatch" % path,
        )
    else:
        require_exact_keys(value["probe"], {"pty"}, "%s probe" % path)
        require_exact_keys(value["probe"]["pty"], PTY_KEYS, "%s pty" % path)
        require_sha256(
            value["probe"]["pty"]["roundtrip_sha256"],
            "%s pty roundtrip_sha256" % path,
        )
        for name in ("character_devices", "isatty"):
            require(
                value["probe"]["pty"][name] is True,
                "%s pty %s did not pass" % (path, name),
            )
        require(
            value["probe"]["pty"]["roundtrip_sha256"]
            == "8d6d22b3644e6c07099e253b687957c6beeea318c584f575877b571a87af5a53",
            "%s PTY payload digest mismatch" % path,
        )


def validate_runtime_result(
    report,
    expected_tier,
    context,
    compile_report,
    compile_report_sha256,
    target,
    version,
):
    path = "%s runtime result" % expected_tier
    require_exact_keys(report, RUNTIME_KEYS, path)
    require(report["qualification_schema_version"] == 2, "%s schema mismatch" % path)
    require(report["report_kind"] == "crossforge-cpython-runtime", "%s kind mismatch" % path)
    require(report["target"] == target, "%s target mismatch" % path)
    require(report["version"] == version, "%s version mismatch" % path)
    require(report["adapter"] == context["adapter"], "%s adapter mismatch" % path)
    require(report["tier"] == expected_tier, "%s tier mismatch" % path)
    require(report["status"] == "passed", "%s did not pass" % path)

    hashes = (
        ("release_sha256", context["release_sha256"]),
        ("compile_report_sha256", compile_report_sha256),
        ("python_sha256", compile_report["python_sha256"]),
        ("extension_sha256", compile_report["extension"]["sha256"]),
    )
    for name, expected in hashes:
        require_sha256(report[name], "%s %s" % (path, name))
        require(report[name] == expected, "%s %s mismatch" % (path, name))
    require_sha256(report["probe_sha256"], "%s probe_sha256" % path)

    runtime = report["runtime"]
    require_exact_keys(runtime, RUNTIME_IDENTITY_KEYS, "%s runtime" % path)
    expected_runtime_kind = (
        "locked-sysroot" if expected_tier == "locked-sysroot" else "clean-rocky-overlay"
    )
    require(
        runtime["kind"] == expected_runtime_kind,
        "%s runtime kind mismatch" % path,
    )
    for name in ("identity_sha256", "os_release_sha256", "loader_sha256"):
        require_sha256(runtime[name], "%s runtime %s" % (path, name))
    if expected_tier == "locked-sysroot":
        require(
            runtime["identity_sha256"] == context["sysroot_sha256"],
            "%s runtime identity differs from the locked sysroot" % path,
        )
        require(runtime["overlay_evidence"] is None, "%s has overlay evidence" % path)
    else:
        require(
            runtime["identity_sha256"] != context["sysroot_sha256"],
            "%s clean runtime reused the locked sysroot identity" % path,
        )
        overlay = validate_overlay_evidence(
            runtime["overlay_evidence"],
            context,
            compile_report,
            target,
            path + " overlay_evidence",
        )
        require(
            runtime["identity_sha256"] == overlay["identity_sha256"],
            "%s overlay identity mismatch" % path,
        )
        require(
            runtime["os_release_sha256"]
            == overlay["runtime_inventory"]["os_release_sha256"],
            "%s overlay os-release mismatch" % path,
        )

    executor = report["executor"]
    require_exact_keys(executor, EXECUTOR_KEYS, "%s executor" % path)
    if target == "aarch64-unknown-linux-gnu":
        for name in ("kind", "version", "cpu", "uname_release"):
            require_string(executor[name], "%s executor %s" % (path, name))
        require_sha256(executor["binary_sha256"], "%s executor binary_sha256" % path)
        try:
            release_executor = context["release"]["qemu"]["executor"]
            expected_executor = {
                "binary_sha256": release_executor["binary_sha256"],
                "version": context["release"]["qemu"]["version"],
                "cpu": release_executor["cpu"],
                "uname_release": release_executor["uname_release"],
            }
        except (KeyError, TypeError) as error:
            raise FinalizationError("release QEMU executor is incomplete") from error
        require(executor["kind"] == "explicit-qemu", "%s must use explicit QEMU" % path)
        for name, expected in expected_executor.items():
            require(executor[name] == expected, "%s executor %s mismatch" % (path, name))
    else:
        require(
            executor
            == {
                "kind": "native-chroot",
                "binary_sha256": None,
                "version": None,
                "cpu": None,
                "uname_release": None,
            },
            "%s must use the native chroot executor" % path,
        )

    dependencies = report["loader_dependencies"]
    require(
        isinstance(dependencies, list)
        and all(isinstance(item, str) and item for item in dependencies),
        "%s loader_dependencies must be a string array" % path,
    )
    require(dependencies, "%s loader_dependencies must not be empty" % path)
    require(
        dependencies == sorted(set(dependencies)),
        "%s loader_dependencies are not canonical" % path,
    )
    require(
        not any("not found" in item or "(0x" in item for item in dependencies),
        "%s loader_dependencies contain unresolved or unstable evidence" % path,
    )
    require(
        not any("libzstd.so" in item for item in dependencies),
        "%s loader_dependencies contain dynamic libzstd" % path,
    )
    device_dependencies = report["device_loader_dependencies"]
    require(
        isinstance(device_dependencies, list)
        and device_dependencies == sorted(set(device_dependencies))
        and device_dependencies,
        "%s device_loader_dependencies are not canonical" % path,
    )
    require(
        not any("not found" in item or "(0x" in item for item in device_dependencies),
        "%s device loader evidence is unstable" % path,
    )
    require(
        not any("libzstd.so" in item for item in device_dependencies),
        "%s device loader dependencies contain dynamic libzstd" % path,
    )
    loaded_objects = report["device_loaded_objects"]
    require(
        isinstance(loaded_objects, list)
        and loaded_objects == sorted(set(loaded_objects))
        and loaded_objects,
        "%s device_loaded_objects are not canonical" % path,
    )
    required_libraries = {
        "libbz2.so.1",
        "libcrypto.so.1.1",
        "libffi.so.6",
        "liblzma.so.5",
        "libsqlite3.so.0",
        "libssl.so.1.1",
        "libuuid.so.1",
        "libz.so.1",
    }
    require(
        required_libraries.issubset(
            {Path(item).name for item in loaded_objects}
        ),
        "%s device loaded-object evidence is incomplete" % path,
    )
    loaded_basenames = {Path(item).name for item in loaded_objects}
    require(
        not any(name.startswith("libzstd.so") for name in loaded_basenames),
        "%s loaded a dynamic libzstd" % path,
    )
    compact_minor = "".join(version.split(".")[:2])
    target_arch = "x86_64" if target.startswith("x86_64-") else "aarch64"
    expected_zstd_modules = (
        {"_zstd.cpython-%s-%s-linux-gnu.so" % (compact_minor, target_arch)}
        if context["zstd"]
        else set()
    )
    zstd_modules = {name for name in loaded_basenames if name.startswith("_zstd.")}
    require(
        zstd_modules == expected_zstd_modules,
        "%s _zstd loaded-object policy mismatch" % path,
    )
    validate_probe(
        report["probe"],
        "%s probe" % path,
        target,
        version,
        "core",
        context["zstd"],
        context["hash_algorithm"],
    )
    validate_probe(
        report["device_probe"],
        "%s device_probe" % path,
        target,
        version,
        "devices",
        context["zstd"],
        context["hash_algorithm"],
    )
    return report


def validate_qualification_zstd(report, release, target, version):
    """Validate and return the normalized final/compile zstd evidence.

    Row aggregation uses this stable boundary after validating the surrounding
    qualification report, then independently recomputes the on-disk ELF facts.
    """
    require(isinstance(report, dict), "qualification report must be an object")
    require("compile" in report and "zstd" in report, "qualification zstd evidence is missing")
    context = release_context(release, target, version)
    context["release"] = release
    evidence = validate_compile_zstd_evidence(
        report["compile"].get("zstd"),
        context,
        report["compile"],
        target,
        version,
    )
    require(
        report["zstd"] == evidence,
        "qualification report zstd evidence mismatch",
    )
    return evidence


def validate_final_report(report, release, target, version):
    require_exact_keys(report, FINAL_REPORT_KEYS, "qualification report")
    require(
        report["qualification_schema_version"] == 2
        and report["report_kind"] == "crossforge-cpython-qualification"
        and report["status"] == "passed",
        "qualification report identity mismatch",
    )
    require(report["target"] == target, "qualification report target mismatch")
    require(report["version"] == version, "qualification report version mismatch")

    context = release_context(release, target, version)
    context["release"] = release
    compile_report = validate_compile_report(
        report["compile"], context, target, version
    )
    compile_digest = serialized_sha256(compile_report)
    require(
        report["compile_report_sha256"] == compile_digest,
        "qualification report compile serialization mismatch",
    )
    validate_qualification_zstd(report, release, target, version)
    require_exact_keys(report["executions"], RUNTIME_TIERS, "qualification executions")
    locked = validate_runtime_result(
        report["executions"]["locked-sysroot"],
        "locked-sysroot",
        context,
        compile_report,
        compile_digest,
        target,
        version,
    )
    clean = validate_runtime_result(
        report["executions"]["clean-rocky"],
        "clean-rocky",
        context,
        compile_report,
        compile_digest,
        target,
        version,
    )
    require_exact_keys(
        report["runtime_result_sha256"],
        RUNTIME_TIERS,
        "qualification runtime_result_sha256",
    )
    require(
        report["runtime_result_sha256"]
        == {
            "locked-sysroot": serialized_sha256(locked),
            "clean-rocky": serialized_sha256(clean),
        },
        "qualification runtime serialization mismatch",
    )
    require(locked["executor"] == clean["executor"], "runtime executor mismatch")
    require(
        locked["python_sha256"] == clean["python_sha256"]
        and locked["extension_sha256"] == clean["extension_sha256"]
        and locked["probe_sha256"] == clean["probe_sha256"],
        "runtime tiers used different artifacts",
    )
    expected = {
        "adapter": context["adapter"],
        "release_sha256": context["release_sha256"],
        "source": compile_report["source"],
        "sysroot_sha256": context["sysroot_sha256"],
        "python_sha256": compile_report["python_sha256"],
        "extension_sha256": compile_report["extension"]["sha256"],
        "probe_sha256": locked["probe_sha256"],
    }
    for name, value in expected.items():
        require(report[name] == value, "qualification report %s mismatch" % name)
    return report


def finalize(compile_path, locked_path, clean_path, release_path, target, version):
    release = load_json(release_path)
    context = release_context(release, target, version)
    context["release"] = release
    compile_report = validate_compile_report(
        load_json(compile_path), context, target, version
    )
    compile_digest = sha256_file(compile_path)
    locked = validate_runtime_result(
        load_json(locked_path),
        "locked-sysroot",
        context,
        compile_report,
        compile_digest,
        target,
        version,
    )
    clean = validate_runtime_result(
        load_json(clean_path),
        "clean-rocky",
        context,
        compile_report,
        compile_digest,
        target,
        version,
    )
    require(
        locked["executor"] == clean["executor"],
        "runtime tiers used different executors",
    )
    require(
        locked["python_sha256"] == clean["python_sha256"]
        and locked["extension_sha256"] == clean["extension_sha256"]
        and locked["probe_sha256"] == clean["probe_sha256"],
        "runtime tiers used different artifacts",
    )

    report = {
        "qualification_schema_version": 2,
        "report_kind": "crossforge-cpython-qualification",
        "status": "passed",
        "target": target,
        "version": version,
        "adapter": context["adapter"],
        "release_sha256": context["release_sha256"],
        "source": compile_report["source"],
        "sysroot_sha256": context["sysroot_sha256"],
        "python_sha256": compile_report["python_sha256"],
        "extension_sha256": compile_report["extension"]["sha256"],
        "probe_sha256": locked["probe_sha256"],
        "compile_report_sha256": compile_digest,
        "compile": compile_report,
        "zstd": compile_report["zstd"],
        "runtime_result_sha256": {
            "locked-sysroot": sha256_file(locked_path),
            "clean-rocky": sha256_file(clean_path),
        },
        "executions": {
            "locked-sysroot": locked,
            "clean-rocky": clean,
        },
    }
    return validate_final_report(report, release, target, version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-report", type=Path, required=True)
    parser.add_argument("--locked-sysroot-result", type=Path, required=True)
    parser.add_argument("--clean-runtime-result", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    report = finalize(
        arguments.compile_report,
        arguments.locked_sysroot_result,
        arguments.clean_runtime_result,
        arguments.release,
        arguments.target,
        arguments.version,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(arguments.output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(arguments.output)
    print("finalized CPython qualification: %s" % arguments.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FinalizationError, KeyError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)

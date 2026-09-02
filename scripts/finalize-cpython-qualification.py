#!/usr/bin/env python3
"""Bind static and dual-runtime CPython qualification evidence."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


class FinalizationError(RuntimeError):
    pass


COMPILE_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "target",
    "version",
    "release_sha256",
    "source",
    "sysroot_sha256",
    "sysroot_transaction_sha256",
    "target_prefix",
    "build_python",
    "target_exec_guard",
    "python_sha256",
    "extension",
    "required_modules",
    "sysconfig",
    "sdk_tree",
    "elf_audit",
}
COMPILE_SOURCE_KEYS = {
    "url",
    "size",
    "sha256",
    "sigstore_bundle_sha256",
    "sigstore_verification",
}
BUILD_PYTHON_KEYS = {"path", "version", "sha256"}
EXTENSION_KEYS = {"name", "sha256"}
SDK_TREE_KEYS = {"entries", "canonical_sha256"}
ELF_AUDIT_KEYS = {"needed", "required_versions", "sha256"}
TARGET_EXEC_GUARD_KEYS = {
    "canary_observed",
    "denied_attempts",
    "canonical_sha256",
}

RUNTIME_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "target",
    "version",
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
    "semaphore": {"acquire_release", "get_value"},
    "network": {"address", "family", "port"},
    "timezone": {"posix_rule", "tzset", "utc_epoch"},
    "wchar": {"code_points", "cpython_api", "wchar_bytes"},
}
PTY_KEYS = {"character_devices", "isatty", "roundtrip_sha256"}

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
VERSION = re.compile(r"3\.[0-9]+\.[0-9]+\Z")


def require(condition, message):
    if not condition:
        raise FinalizationError(message)


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


def release_context(release, target, version):
    require(target in TARGETS, "unsupported CPython target: %s" % target)
    require(VERSION.fullmatch(version) is not None, "invalid CPython version: %s" % version)

    try:
        targets = [item for item in release["targets"] if item["triple"] == target]
        versions = [
            item for item in release["python"]["versions"] if item["version"] == version
        ]
    except (KeyError, TypeError) as error:
        raise FinalizationError("release.json has an invalid qualification contract") from error
    require(len(targets) == 1, "release.json must select exactly one target")
    require(len(versions) == 1, "release.json must select exactly one CPython version")
    source = versions[0].get("source")
    require(isinstance(source, dict), "release CPython source must be an object")
    require(source.get("status") == "locked", "release CPython source is not locked")
    sysroot = targets[0].get("sysroot")
    require(isinstance(sysroot, dict), "release target sysroot must be an object")
    require(sysroot.get("status") == "locked", "release target sysroot is not locked")
    return {
        "release_sha256": canonical_sha256(release),
        "source": source,
        "sysroot_sha256": require_sha256(
            sysroot.get("canonical_sha256"),
            "release target sysroot canonical_sha256",
        ),
    }


def validate_dynamic_map(value, path):
    require(isinstance(value, dict), "%s must be an object" % path)
    for key in value:
        require_string(key, "%s key" % path)


def validate_overlay_evidence(value, context, target, path):
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
        identity["sysroot"]["lock_sha256"] == context["sysroot_sha256"],
        "%s sysroot lock mismatch" % path,
    )
    require_sha256(identity["sysroot"]["transaction_sha256"], path + " transaction")
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
        report["qualification_schema_version"] == 1,
        "compile report schema version mismatch",
    )
    require(
        report["report_kind"] == "crossforge-cpython-compile",
        "compile report kind mismatch",
    )
    require(report["target"] == target, "compile report target mismatch")
    require(report["version"] == version, "compile report version mismatch")
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

    build_python = report["build_python"]
    require_exact_keys(build_python, BUILD_PYTHON_KEYS, "compile report build_python")
    require_string(build_python["path"], "compile report build_python path")
    require(
        build_python["version"] == version,
        "compile report build Python version mismatch",
    )
    require_sha256(build_python["sha256"], "compile report build_python sha256")

    guard = report["target_exec_guard"]
    require_exact_keys(guard, TARGET_EXEC_GUARD_KEYS, "compile report target_exec_guard")
    require(
        guard["canary_observed"] is True
        and type(guard["denied_attempts"]) is int
        and guard["denied_attempts"] > 0,
        "compile report target-execution guard did not prove enforcement",
    )
    require_sha256(
        guard["canonical_sha256"],
        "compile report target_exec_guard canonical_sha256",
    )

    require_string(report["target_prefix"], "compile report target_prefix")
    minor = ".".join(version.split(".")[:2])
    expected_prefix = "/opt/crossforge/python/cp%s/targets/%s" % (
        minor.replace(".", ""),
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
    require_sha256(extension["sha256"], "compile report extension sha256")

    modules = report["required_modules"]
    require_exact_keys(modules, REQUIRED_MODULES, "compile report required_modules")
    for module, relative_path in modules.items():
        require_string(relative_path, "compile report required module %s" % module)
        require(
            not relative_path.startswith("/")
            and ".." not in Path(relative_path).parts,
            "compile report required module path is unsafe: %s" % module,
        )
    validate_dynamic_map(report["sysconfig"], "compile report sysconfig")
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
    for name, audit in report["elf_audit"].items():
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
        validate_dynamic_map(
            audit["required_versions"],
            "compile report elf_audit %s required_versions" % name,
        )
        require_sha256(audit["sha256"], "compile report elf_audit %s sha256" % name)
    require(
        set(modules.values()).issubset(report["elf_audit"]),
        "compile report required modules are not all present in the ELF audit",
    )
    require(
        report["python_sha256"]
        in {audit["sha256"] for audit in report["elf_audit"].values()},
        "compile report Python hash is absent from the ELF audit",
    )
    require(
        extension["sha256"]
        in {audit["sha256"] for audit in report["elf_audit"].values()},
        "compile report extension hash is absent from the ELF audit",
    )
    return report


def validate_probe(value, path, target, version, expected_mode):
    expected_keys = CORE_PROBE_KEYS if expected_mode == "core" else DEVICE_PROBE_KEYS
    require_exact_keys(value, expected_keys, path)
    require(value["schema_version"] == 1, "%s schema mismatch" % path)
    require(value["report_kind"] == "crossforge-cpython-probe", "%s kind mismatch" % path)
    require(value["mode"] == expected_mode, "%s mode mismatch" % path)
    require(value["status"] == "passed", "%s did not pass" % path)
    require(value["target"] == target, "%s target mismatch" % path)
    require(value["version"] == version, "%s version mismatch" % path)

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
            imports == REQUIRED_PROBE_IMPORTS,
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
            and value["extension"]["file"].startswith("_crossforge.cpython-313-")
            and value["extension"]["file"].endswith(".so"),
            "%s extension evidence mismatch" % path,
        )
        require(
            value["hash_algorithm"]
            == {"algorithm": "siphash13", "hash_bits": 64, "seed_bits": 128},
            "%s hash algorithm evidence mismatch" % path,
        )
        require(
            value["threading"] == {"event": True, "result": 5050}
            and value["semaphore"]
            == {"acquire_release": True, "get_value": True},
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
    require(report["qualification_schema_version"] == 1, "%s schema mismatch" % path)
    require(report["report_kind"] == "crossforge-cpython-runtime", "%s kind mismatch" % path)
    require(report["target"] == target, "%s target mismatch" % path)
    require(report["version"] == version, "%s version mismatch" % path)
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
    validate_probe(report["probe"], "%s probe" % path, target, version, "core")
    validate_probe(
        report["device_probe"],
        "%s device_probe" % path,
        target,
        version,
        "devices",
    )
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

    return {
        "qualification_schema_version": 1,
        "report_kind": "crossforge-cpython-qualification",
        "status": "passed",
        "target": target,
        "version": version,
        "release_sha256": context["release_sha256"],
        "source": compile_report["source"],
        "sysroot_sha256": context["sysroot_sha256"],
        "python_sha256": compile_report["python_sha256"],
        "extension_sha256": compile_report["extension"]["sha256"],
        "probe_sha256": locked["probe_sha256"],
        "compile_report_sha256": compile_digest,
        "compile": compile_report,
        "runtime_result_sha256": {
            "locked-sysroot": sha256_file(locked_path),
            "clean-rocky": sha256_file(clean_path),
        },
        "executions": {
            "locked-sysroot": locked,
            "clean-rocky": clean,
        },
    }


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

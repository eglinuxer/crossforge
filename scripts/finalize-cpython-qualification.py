#!/usr/bin/env python3
"""Bind static and dual-runtime CPython qualification evidence."""

import argparse
import hashlib
import json
import re
import runpy
import sys
from pathlib import Path, PurePosixPath


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
    str(Path(__file__).with_name("release-components-core.py"))
)
ProjectionError = RELEASE_COMPONENTS["ProjectionError"]
ZSTD_EVIDENCE = runpy.run_path(
    str(Path(__file__).with_name("python_zstd_evidence.py"))
)
ZstdEvidenceError = ZSTD_EVIDENCE["ZstdEvidenceError"]
ABI_CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("abi_contract.py"))
)
AbiContractError = ABI_CONTRACT["AbiContractError"]
PYTHON_ABI = runpy.run_path(
    str(Path(__file__).with_name("python_abi_audit.py"))
)
PythonAbiAuditError = PYTHON_ABI["PythonAbiAuditError"]
RUNTIME_PROVIDERS = runpy.run_path(
    str(Path(__file__).with_name("python_runtime_providers.py"))
)
RuntimeProviderPolicyError = RUNTIME_PROVIDERS[
    "RuntimeProviderPolicyError"
]


COMPILE_KEYS = {
    "qualification_schema_version",
    "report_kind",
    "target",
    "version",
    "adapter",
    "release_sha256",
    "qualification_components",
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
    "abi",
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
ELF_AUDIT_KEYS = {
    "needed",
    "sha256",
    "elf_record_sha256",
    "elf_record",
    "elf_policy",
    "ownership",
}
COMPILE_ABI_KEYS = {
    "baseline",
    "provider_manifest",
    "sysroot_inventory",
    "runtime_provider_policy",
    "provider_catalog",
    "python_global",
}
ELF_POLICY_RESULT_KEYS = {
    "artifact",
    "profile",
    "elf_kind",
    "not_applicable",
    "properties",
    "used_exceptions",
}
ELF_PROPERTY_KEYS = {
    "dynamic_section",
    "textrel",
    "relr",
    "rpath",
    "runpath",
    "path_tags",
    "pie",
    "bind_now",
    "interpreter",
    "gnu_stack_present",
    "gnu_stack_executable",
    "writable_executable_segments",
    "relro",
    "machine",
    "elf_class",
    "elf_data",
    "elf_type",
}
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
    "runtime_providers",
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
    "qualification_components",
    "source",
    "sysroot_sha256",
    "python_sha256",
    "extension_sha256",
    "probe_sha256",
    "compile_report_sha256",
    "compile",
    "abi",
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


def validate_python_qualification_components(value, release, path):
    try:
        return RELEASE_COMPONENTS["validate_python_qualification_components"](
            value, release
        )
    except ProjectionError as error:
        raise FinalizationError("%s: %s" % (path, error)) from error


def load_abi_context(
    target,
    baseline_path,
    provider_manifest_path,
    sysroot_inventory_path,
    runtime_provider_policy_path,
    provider_catalog_path,
):
    """Load the reviewed ABI inputs used to revalidate compile evidence."""
    arch = "x86_64" if target.startswith("x86_64-") else "aarch64"
    try:
        baseline = ABI_CONTRACT["load_baseline"](
            baseline_path, expected_arch=arch, expected_triple=target
        )
        manifest = ABI_CONTRACT["load_provider_manifest"](
            provider_manifest_path
        )
        inventory = ABI_CONTRACT["load_inventory"](
            sysroot_inventory_path,
            expected_arch=arch,
            expected_triple=target,
        )
        ABI_CONTRACT["validate_inventory_provider_manifest"](
            inventory, manifest
        )
        ABI_CONTRACT["validate_inventory_superset"](inventory, baseline)
    except AbiContractError as error:
        raise FinalizationError(str(error)) from error
    require(
        inventory["source"]["kind"] == "locked-sysroot",
        "ABI inventory is not a locked sysroot inventory",
    )
    for path, document, label in (
        (baseline_path, baseline, "ABI baseline"),
        (sysroot_inventory_path, inventory, "ABI sysroot inventory"),
    ):
        require(
            path.read_bytes()
            == ABI_CONTRACT["canonical_bytes"](document) + b"\n",
            "%s is not canonical JSON" % label,
        )

    try:
        policy = RUNTIME_PROVIDERS["load_json"](
            runtime_provider_policy_path
        )
        runtime_target = RUNTIME_PROVIDERS["policy_target"](
            policy, arch, target
        )
        runtime_evidence = RUNTIME_PROVIDERS[
            "runtime_provider_evidence"
        ](policy, arch)
        reviewed_provider_catalog = RUNTIME_PROVIDERS["load_json"](
            provider_catalog_path
        )
    except RuntimeProviderPolicyError as error:
        raise FinalizationError(str(error)) from error
    require(
        provider_catalog_path.read_bytes()
        == RUNTIME_PROVIDERS["canonical_bytes"](
            reviewed_provider_catalog
        )
        + b"\n",
        "reviewed Python provider catalog is not canonical JSON",
    )
    external_providers = [
        provider["soname"] for provider in runtime_target["providers"]
    ]
    try:
        PYTHON_ABI["validate_provider_catalog"](
            baseline,
            external_providers,
            reviewed_provider_catalog,
        )
    except PythonAbiAuditError as error:
        raise FinalizationError(str(error)) from error
    require(
        RUNTIME_PROVIDERS["canonical_sha256"](
            reviewed_provider_catalog
        )
        == runtime_evidence["provider_catalog_sha256"],
        "reviewed Python provider catalog differs from policy",
    )

    manifest_sha256 = ABI_CONTRACT["canonical_sha256"](manifest)
    baseline_sha256 = ABI_CONTRACT["canonical_sha256"](baseline)
    inventory_sha256 = ABI_CONTRACT["canonical_sha256"](inventory)
    expected_identities = {
        "baseline": {
            "file": "abi/el8/%s.json" % arch,
            "canonical_sha256": baseline_sha256,
            "source_inventory": baseline["review"]["source_inventory"],
            "source_inventory_sha256": baseline["review"][
                "source_inventory_sha256"
            ],
        },
        "provider_manifest": {
            "file": "config/abi-providers.json",
            "canonical_sha256": manifest_sha256,
        },
        "sysroot_inventory": {
            "file": "evidence/abi/el8-%s-sysroot.json" % arch,
            "canonical_sha256": inventory_sha256,
            "source": dict(inventory["source"]),
        },
        "runtime_provider_policy": {
            "file": "config/python-runtime-providers.json",
            "canonical_sha256": runtime_evidence["policy_sha256"],
            "sysroot_lock_sha256": runtime_evidence[
                "sysroot_lock_sha256"
            ],
            "provider_catalog_sha256": runtime_evidence[
                "provider_catalog_sha256"
            ],
        },
    }
    expected_providers = []
    for soname, provider in inventory["providers"].items():
        expected_providers.append(
            {
                "soname": soname,
                "path": provider["path"],
                "source": "frozen-core",
                "dso_sha256": provider["sha256"],
                "rpm_owner": None,
            }
        )
    for provider in runtime_evidence["providers"]:
        expected_providers.append(
            {
                "soname": provider["soname"],
                "path": provider["path"],
                "source": "python-runtime",
                "dso_sha256": provider["dso_sha256"],
                "rpm_owner": dict(provider["owner"]),
            }
        )
    expected_providers.sort(key=lambda item: item["soname"])
    return {
        "arch": arch,
        "target": target,
        "baseline": baseline,
        "external_providers": external_providers,
        "runtime_provider_evidence": runtime_evidence,
        "reviewed_provider_catalog": reviewed_provider_catalog,
        "reviewed_provider_catalog_file": (
            "evidence/abi/el8-%s-python-provider-catalog.json" % arch
        ),
        "expected_identities": expected_identities,
        "expected_providers": expected_providers,
        "machine": ABI_CONTRACT["TARGETS"][arch]["readelf_machine"],
        "interpreter": ABI_CONTRACT["TARGETS"][arch]["interpreter"],
    }


def validate_compile_abi(value, report, context, minor):
    path = "compile report abi"
    require_exact_keys(value, COMPILE_ABI_KEYS, path)
    for name in (
        "baseline",
        "provider_manifest",
        "sysroot_inventory",
        "runtime_provider_policy",
    ):
        require(
            value[name] == context["expected_identities"][name],
            "%s %s identity mismatch" % (path, name),
        )

    catalog = value["provider_catalog"]
    require_exact_keys(
        catalog,
        {
            "file",
            "provider_count",
            "elf_records_sha256",
            "records",
            "providers",
        },
        path + " provider_catalog",
    )
    require(
        catalog["file"] == context["reviewed_provider_catalog_file"],
        "%s provider catalog file identity mismatch" % path,
    )
    expected = context["expected_providers"]
    require(
        type(catalog["provider_count"]) is int
        and catalog["provider_count"] == len(expected),
        "%s provider count mismatch" % path,
    )
    require_sha256(
        catalog["elf_records_sha256"], path + " provider catalog digest"
    )
    records = catalog["records"]
    try:
        PYTHON_ABI["validate_provider_catalog"](
            context["baseline"],
            context["external_providers"],
            records,
        )
    except PythonAbiAuditError as error:
        raise FinalizationError(
            "%s provider catalog: %s" % (path, error)
        ) from error
    require(
        catalog["elf_records_sha256"]
        == RUNTIME_PROVIDERS["canonical_sha256"](records)
        == context["runtime_provider_evidence"][
            "provider_catalog_sha256"
        ]
        and records == context["reviewed_provider_catalog"],
        "%s provider catalog digest differs from the reviewed policy" % path,
    )
    providers = catalog["providers"]
    require(
        isinstance(providers, list) and len(providers) == len(expected),
        "%s provider summaries differ" % path,
    )
    for index, (observed, expected_provider) in enumerate(
        zip(providers, expected)
    ):
        require_exact_keys(
            observed,
            {
                "soname",
                "path",
                "source",
                "dso_sha256",
                "elf_record_sha256",
                "rpm_owner",
            },
            "%s provider %d" % (path, index),
        )
        require(
            {
                key: observed[key]
                for key in (
                    "soname",
                    "path",
                    "source",
                    "dso_sha256",
                    "rpm_owner",
                )
            }
            == expected_provider,
            "%s provider %d identity mismatch" % (path, index),
        )
        require_sha256(
            observed["elf_record_sha256"],
            "%s provider %d ELF record digest" % (path, index),
        )
        require(
            observed["elf_record_sha256"]
            == RUNTIME_PROVIDERS["canonical_sha256"](
                records[observed["soname"]]
            ),
            "%s provider %d ELF record digest mismatch" % (path, index),
        )

    python_global = value["python_global"]
    require_exact_keys(
        python_global,
        {
            "identity",
            "sha256",
            "elf_record_sha256",
            "needed",
            "default_export_count",
            "record",
        },
        path + " python_global",
    )
    expected_identity = "bin/python%s" % minor
    require(
        python_global["identity"] == expected_identity
        and python_global["sha256"] == report["python_sha256"],
        "%s Python global identity mismatch" % path,
    )
    require_sha256(
        python_global["elf_record_sha256"],
        path + " Python global ELF record digest",
    )
    try:
        PYTHON_ABI["validate_elf_record"](python_global["record"])
    except PythonAbiAuditError as error:
        raise FinalizationError(
            "%s Python global record: %s" % (path, error)
        ) from error
    require(
        python_global["record"]["identity"] == expected_identity
        and python_global["record"]["soname"] is None
        and python_global["elf_record_sha256"]
        == RUNTIME_PROVIDERS["canonical_sha256"](
            python_global["record"]
        )
        and python_global["needed"] == python_global["record"]["needed"]
        and python_global["default_export_count"]
        == len(python_global["record"]["default_exports"]),
        "%s Python global ELF record binding mismatch" % path,
    )
    require(
        isinstance(python_global["needed"], list)
        and len(python_global["needed"])
        == len(set(python_global["needed"]))
        and all(
            isinstance(item, str) and item and "/" not in item
            for item in python_global["needed"]
        ),
        "%s Python global DT_NEEDED is invalid" % path,
    )
    require(
        type(python_global["default_export_count"]) is int
        and python_global["default_export_count"] > 0,
        "%s Python global export count is invalid" % path,
    )
    return value


def validate_release_abi_context(release, context):
    """Bind finalizer ABI inputs to the canonical release identities."""
    arch = context["arch"]
    try:
        expected = ABI_CONTRACT["release_abi_inputs"](release, arch)
    except AbiContractError as error:
        raise FinalizationError(str(error)) from error
    identities = context["expected_identities"]
    observed = {
        "provider_manifest": {
            "file": identities["provider_manifest"]["file"],
            "canonical_sha256": identities["provider_manifest"][
                "canonical_sha256"
            ],
        },
        "baseline": {
            "file": identities["baseline"]["file"],
            "canonical_sha256": identities["baseline"][
                "canonical_sha256"
            ],
        },
        "sysroot_inventory": {
            "file": identities["sysroot_inventory"]["file"],
            "canonical_sha256": identities["sysroot_inventory"][
                "canonical_sha256"
            ],
        },
        "runtime_provider_policy": {
            "file": identities["runtime_provider_policy"]["file"],
            "canonical_sha256": identities["runtime_provider_policy"][
                "canonical_sha256"
            ],
        },
        "provider_catalog": {
            "file": context["reviewed_provider_catalog_file"],
            "canonical_sha256": context[
                "runtime_provider_evidence"
            ]["provider_catalog_sha256"],
        },
    }
    require(
        observed == expected,
        "finalizer ABI inputs differ from release.json",
    )
    return expected


def validate_elf_policy_result(
    value, name, context, expected_executable
):
    path = "compile report elf_audit %s elf_policy" % name
    require_exact_keys(value, ELF_POLICY_RESULT_KEYS, path)
    require(
        value["artifact"] == name
        and value["profile"] == "crossforge-qualified-v1"
        and value["used_exceptions"] == [],
        "%s identity/profile/exception mismatch" % path,
    )
    properties = value["properties"]
    require_exact_keys(properties, ELF_PROPERTY_KEYS, path + " properties")
    require(
        properties["machine"] == context["machine"]
        and properties["elf_class"] == "ELF64"
        and properties["elf_data"] == "2's complement, little endian"
        and properties["dynamic_section"] is True
        and properties["textrel"] is False
        and properties["relr"] is False
        and properties["rpath"] == []
        and properties["runpath"] == []
        and properties["path_tags"] == []
        and type(properties["pie"]) is bool
        and properties["bind_now"] is True
        and properties["gnu_stack_present"] is True
        and properties["gnu_stack_executable"] is False
        and properties["writable_executable_segments"] is False
        and properties["relro"] is True,
        "%s violates the qualified ELF profile" % path,
    )
    require(
        type(expected_executable) is bool,
        "%s expected role is invalid" % path,
    )
    if expected_executable:
        require(
            value["elf_kind"] == "dynamic-executable"
            and value["not_applicable"] == []
            and properties["interpreter"] == context["interpreter"],
            "%s executable interpreter mismatch" % path,
        )
    else:
        require(
            value["elf_kind"] == "shared-object"
            and value["not_applicable"] == ["interpreter"]
            and properties["interpreter"] is None
            and properties["pie"] is False,
            "%s shared-object classification mismatch" % path,
        )
    return value


def validate_runtime_provider_evidence(value, context, path):
    require(
        value == context["runtime_provider_evidence"],
        "%s differs from the reviewed runtime provider policy" % path,
    )
    return value


def collect_actual_elf_evidence(
    compile_report,
    abi_context,
    target_prefix,
    readelf,
    qualification_extension=None,
):
    """Recompute ELF records and policy from the actual qualified bytes."""
    target_prefix = Path(target_prefix)
    readelf = Path(readelf)
    require(
        target_prefix.is_dir() and not target_prefix.is_symlink(),
        "actual target prefix is missing or unsafe",
    )
    require(readelf.is_file(), "actual readelf is missing")
    resolved_prefix = target_prefix.resolve()
    result = {}
    for name in sorted(compile_report["elf_audit"]):
        require(
            type(name) is str and name,
            "qualified ELF path is invalid",
        )
        if name.startswith("qualification/"):
            if qualification_extension is None:
                continue
            path = Path(qualification_extension)
            require(
                name == "qualification/" + path.name
                and path.is_file()
                and not path.is_symlink(),
                "qualification ELF path differs from compile report",
            )
        else:
            relative = PurePosixPath(name)
            require(
                not relative.is_absolute()
                and ".." not in relative.parts,
                "qualified ELF path is unsafe: %s" % name,
            )
            path = target_prefix.joinpath(*relative.parts)
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise FinalizationError(
                    "qualified ELF is missing or unsafe: %s" % name
                ) from error
            require(
                resolved_prefix in resolved.parents,
                "qualified ELF escapes the target prefix: %s" % name,
            )
        require(
            path.is_file() and not path.is_symlink(),
            "qualified ELF is missing or unsafe: %s" % name,
        )
        try:
            evidence, record = PYTHON_ABI["elf_record_from_file"](
                readelf, path, name
            )
            policy = ABI_CONTRACT["audit_elf_policy"](
                abi_context["baseline"],
                name,
                evidence["dynamic_section"],
                evidence["program_headers"],
                evidence["elf_header"],
                profile_name="crossforge-qualified-v1",
            )
        except (PythonAbiAuditError, AbiContractError) as error:
            raise FinalizationError(
                "actual ELF audit failed for %s: %s" % (name, error)
            ) from error
        result[name] = {
            "sha256": sha256_file(path),
            "elf_record": record,
            "elf_policy": policy,
        }
    expected_sdk = {
        name
        for name in compile_report["elf_audit"]
        if not name.startswith("qualification/")
    }
    actual_sdk = set(result).intersection(expected_sdk)
    require(actual_sdk == expected_sdk, "actual SDK ELF inventory is incomplete")
    return result


def validate_actual_elf_evidence(
    report,
    actual,
    require_qualification_artifact,
):
    require(isinstance(actual, dict), "actual ELF evidence is required")
    expected_names = set(report["elf_audit"])
    qualification_names = {
        name for name in expected_names if name.startswith("qualification/")
    }
    required_names = (
        expected_names
        if require_qualification_artifact
        else expected_names - qualification_names
    )
    require(
        set(actual) == required_names,
        "actual ELF evidence inventory differs from compile report",
    )
    for name in sorted(actual):
        require_exact_keys(
            actual[name],
            {"sha256", "elf_record", "elf_policy"},
            "actual ELF evidence %s" % name,
        )
        declared = report["elf_audit"][name]
        require(
            actual[name]
            == {
                "sha256": declared["sha256"],
                "elf_record": declared["elf_record"],
                "elf_policy": declared["elf_policy"],
            },
            "actual ELF bytes differ from compile evidence: %s" % name,
        )
    return actual


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
        and module["needed"] == sorted(audit["needed"]),
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


def validate_overlay_evidence(
    value, context, compile_report, target, abi_context, path
):
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
    expected_packages_by_name = {
        provider["owner"]["name"]: provider["owner"]
        for provider in abi_context["runtime_provider_evidence"]["providers"]
    }
    expected_packages = [
        expected_packages_by_name[name]
        for name in sorted(expected_packages_by_name)
    ]
    require(
        isinstance(packages, list) and len(packages) == len(expected_packages),
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
        packages == expected_packages,
        "%s packages differ from runtime provider ownership" % path,
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


def validate_compile_report(
    report,
    context,
    target,
    version,
    abi_context,
    actual_elf_evidence,
    require_qualification_artifact,
):
    require_exact_keys(report, COMPILE_KEYS, "compile report")
    require(
        report["qualification_schema_version"] == 4,
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
    validate_python_qualification_components(
        report["qualification_components"],
        context["release"],
        "compile report qualification_components",
    )
    require_sha256(report["sysroot_sha256"], "compile report sysroot_sha256")
    require(
        report["sysroot_sha256"] == context["sysroot_sha256"],
        "compile report sysroot digest mismatch",
    )
    require(
        abi_context["target"] == target
        and abi_context["expected_identities"]["sysroot_inventory"]["source"][
            "identity_sha256"
        ]
        == context["sysroot_sha256"]
        and abi_context["runtime_provider_evidence"][
            "sysroot_lock_sha256"
        ]
        == context["sysroot_sha256"],
        "compile report ABI inputs differ from the release sysroot",
    )
    validate_release_abi_context(context["release"], abi_context)
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
    validate_compile_abi(report["abi"], report, abi_context, minor)
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
            len(audit["needed"]) == len(set(audit["needed"])),
            "compile report ELF needed list repeats a provider: %s" % name,
        )
        require(
            all("/" not in item for item in audit["needed"]),
            "compile report ELF has a path-qualified dependency: %s" % name,
        )
        require_sha256(audit["sha256"], "compile report elf_audit %s sha256" % name)
        require_sha256(
            audit["elf_record_sha256"],
            "compile report elf_audit %s ELF record digest" % name,
        )
        try:
            PYTHON_ABI["validate_elf_record"](audit["elf_record"])
        except PythonAbiAuditError as error:
            raise FinalizationError(
                "compile report ELF record %s: %s" % (name, error)
            ) from error
        require(
            audit["elf_record"]["identity"] == name
            and audit["elf_record_sha256"]
            == RUNTIME_PROVIDERS["canonical_sha256"](
                audit["elf_record"]
            )
            and audit["elf_record"]["needed"] == audit["needed"],
            "compile report ELF record binding mismatch: %s" % name,
        )
        validate_elf_policy_result(
            audit["elf_policy"],
            name,
            abi_context,
            name == python_audit_path,
        )
        try:
            expected_ownership = PYTHON_ABI["audit_python_elf"](
                abi_context["baseline"],
                abi_context["external_providers"],
                report["abi"]["provider_catalog"]["records"],
                report["abi"]["python_global"]["record"],
                audit["elf_record"],
            )
        except PythonAbiAuditError as error:
            raise FinalizationError(
                "compile report ELF ownership %s: %s" % (name, error)
            ) from error
        require(
            audit["ownership"] == expected_ownership,
            "compile report ELF ownership binding mismatch: %s" % name,
        )
    python_global = report["abi"]["python_global"]
    python_audit = report["elf_audit"][python_audit_path]
    require(
        python_global["identity"] == python_audit_path
        and python_global["sha256"] == python_audit["sha256"]
        and python_global["elf_record_sha256"]
        == python_audit["elf_record_sha256"]
        and python_global["record"] == python_audit["elf_record"]
        and python_global["needed"] == python_audit["needed"],
        "compile report Python global record differs from the actual Python audit",
    )
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
    validate_actual_elf_evidence(
        report,
        actual_elf_evidence,
        require_qualification_artifact,
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
    abi_context,
):
    path = "%s runtime result" % expected_tier
    require_exact_keys(report, RUNTIME_KEYS, path)
    require(report["qualification_schema_version"] == 3, "%s schema mismatch" % path)
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
            abi_context,
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

    validate_runtime_provider_evidence(
        report["runtime_providers"],
        abi_context,
        path + " runtime_providers",
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
        provider["soname"]
        for provider in abi_context["runtime_provider_evidence"]["providers"]
    }
    runtime_root = (
        "/runtime-locked"
        if expected_tier == "locked-sysroot"
        else "/runtime-clean"
    )
    provider_paths = {
        provider["soname"]: str(
            PurePosixPath(runtime_root)
            / provider["path"].lstrip("/")
        )
        for provider in abi_context["expected_providers"]
    }
    loaded_by_soname = {soname: [] for soname in provider_paths}
    for item in loaded_objects:
        soname = Path(item).name
        if soname in provider_paths:
            require(
                item == provider_paths[soname],
                "%s loaded an unreviewed provider path: %s"
                % (path, item),
            )
            loaded_by_soname[soname].append(item)
    require(
        all(
            len(loaded_by_soname[soname]) == 1
            for soname in required_libraries
        )
        and all(len(items) <= 1 for items in loaded_by_soname.values()),
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


def default_abi_context(target):
    repository = Path(__file__).resolve().parents[1]
    arch = "x86_64" if target.startswith("x86_64-") else "aarch64"
    return load_abi_context(
        target,
        repository / ("abi/el8/%s.json" % arch),
        repository / "config/abi-providers.json",
        repository / ("evidence/abi/el8-%s-sysroot.json" % arch),
        repository / "config/python-runtime-providers.json",
        repository
        / ("evidence/abi/el8-%s-python-provider-catalog.json" % arch),
    )


def validate_final_report(
    report,
    release,
    target,
    version,
    abi_context=None,
    actual_elf_evidence=None,
    require_qualification_artifact=True,
):
    if abi_context is None:
        abi_context = default_abi_context(target)
    require_exact_keys(report, FINAL_REPORT_KEYS, "qualification report")
    require(
        report["qualification_schema_version"] == 4
        and report["report_kind"] == "crossforge-cpython-qualification"
        and report["status"] == "passed",
        "qualification report identity mismatch",
    )
    require(report["target"] == target, "qualification report target mismatch")
    require(report["version"] == version, "qualification report version mismatch")

    context = release_context(release, target, version)
    context["release"] = release
    compile_report = validate_compile_report(
        report["compile"],
        context,
        target,
        version,
        abi_context,
        actual_elf_evidence,
        require_qualification_artifact,
    )
    require(
        report["abi"] == compile_report["abi"],
        "qualification report ABI evidence differs from compile report",
    )
    qualification_components = validate_python_qualification_components(
        report["qualification_components"],
        release,
        "qualification report qualification_components",
    )
    require(
        qualification_components == compile_report["qualification_components"],
        "qualification report component identities differ from compile report",
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
        abi_context,
    )
    clean = validate_runtime_result(
        report["executions"]["clean-rocky"],
        "clean-rocky",
        context,
        compile_report,
        compile_digest,
        target,
        version,
        abi_context,
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


def finalize(
    compile_path,
    locked_path,
    clean_path,
    release_path,
    target,
    version,
    abi_baseline_path=None,
    abi_provider_manifest_path=None,
    sysroot_abi_inventory_path=None,
    runtime_provider_policy_path=None,
    provider_catalog_path=None,
    target_prefix=None,
    qualification_extension=None,
    readelf=None,
    actual_elf_evidence=None,
    abi_context_override=None,
):
    release = load_json(release_path)
    context = release_context(release, target, version)
    context["release"] = release
    if abi_context_override is not None:
        abi_context = abi_context_override
    elif abi_baseline_path is None:
        abi_context = default_abi_context(target)
    else:
        require(
            abi_provider_manifest_path is not None
            and sysroot_abi_inventory_path is not None
            and runtime_provider_policy_path is not None
            and provider_catalog_path is not None,
            "finalizer ABI input paths are incomplete",
        )
        abi_context = load_abi_context(
            target,
            abi_baseline_path,
            abi_provider_manifest_path,
            sysroot_abi_inventory_path,
            runtime_provider_policy_path,
            provider_catalog_path,
        )
    unvalidated_compile = load_json(compile_path)
    if actual_elf_evidence is None:
        require(
            target_prefix is not None
            and qualification_extension is not None
            and readelf is not None,
            "finalizer actual ELF inputs are incomplete",
        )
        actual_elf_evidence = collect_actual_elf_evidence(
            unvalidated_compile,
            abi_context,
            target_prefix,
            readelf,
            qualification_extension,
        )
    compile_report = validate_compile_report(
        unvalidated_compile,
        context,
        target,
        version,
        abi_context,
        actual_elf_evidence,
        True,
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
        abi_context,
    )
    clean = validate_runtime_result(
        load_json(clean_path),
        "clean-rocky",
        context,
        compile_report,
        compile_digest,
        target,
        version,
        abi_context,
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
        "qualification_schema_version": 4,
        "report_kind": "crossforge-cpython-qualification",
        "status": "passed",
        "target": target,
        "version": version,
        "adapter": context["adapter"],
        "release_sha256": context["release_sha256"],
        "qualification_components": compile_report[
            "qualification_components"
        ],
        "source": compile_report["source"],
        "sysroot_sha256": context["sysroot_sha256"],
        "python_sha256": compile_report["python_sha256"],
        "extension_sha256": compile_report["extension"]["sha256"],
        "probe_sha256": locked["probe_sha256"],
        "compile_report_sha256": compile_digest,
        "compile": compile_report,
        "abi": compile_report["abi"],
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
    return validate_final_report(
        report,
        release,
        target,
        version,
        abi_context,
        actual_elf_evidence,
        True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-report", type=Path, required=True)
    parser.add_argument("--locked-sysroot-result", type=Path, required=True)
    parser.add_argument("--clean-runtime-result", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--abi-baseline", type=Path, required=True)
    parser.add_argument("--abi-provider-manifest", type=Path, required=True)
    parser.add_argument("--sysroot-abi-inventory", type=Path, required=True)
    parser.add_argument("--runtime-provider-policy", type=Path, required=True)
    parser.add_argument("--python-provider-catalog", type=Path, required=True)
    parser.add_argument("--target-prefix", type=Path, required=True)
    parser.add_argument("--qualification-extension", type=Path, required=True)
    parser.add_argument("--readelf", type=Path, required=True)
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
        arguments.abi_baseline,
        arguments.abi_provider_manifest,
        arguments.sysroot_abi_inventory,
        arguments.runtime_provider_policy,
        arguments.python_provider_catalog,
        arguments.target_prefix,
        arguments.qualification_extension,
        arguments.readelf,
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

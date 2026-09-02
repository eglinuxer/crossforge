#!/usr/bin/env python3
"""Strict ABI baseline, inventory, and GNU readelf audit primitives.

The reviewed baseline contains only exact, provider-keyed public versioned
exports.  Undefined unversioned symbols are deliberately returned under a
separate ``recorded-not-allowlisted`` disposition: they are evidence for a
later dependency-ownership check, never members of the core ABI allowlist.

This module uses only Python 3.6-compatible standard-library interfaces so it
can run in the locked EL8 host environment.
"""

import hashlib
import json
import math
import posixpath
import re
from pathlib import Path


BASELINE_SCHEMA_ID = "https://crossforge.dev/schemas/abi-baseline.schema.json"
INVENTORY_SCHEMA_ID = "https://crossforge.dev/schemas/abi-inventory.schema.json"
PROVIDER_MANIFEST_SCHEMA_ID = (
    "https://crossforge.dev/schemas/abi-providers.schema.json"
)
BASELINE_KIND = "crossforge-abi-baseline"
INVENTORY_KIND = "crossforge-abi-inventory"
PROVIDER_MANIFEST_KIND = "crossforge-abi-provider-manifest"
BASELINE_KEYS = {
    "$schema",
    "schema_version",
    "kind",
    "baseline",
    "target",
    "review",
    "providers",
    "elf_policy",
}
INVENTORY_KEYS = {
    "$schema",
    "schema_version",
    "kind",
    "target",
    "source",
    "providers",
}
TARGET_KEYS = {"arch", "triple"}
REVIEW_KEYS = {"status", "source_inventory", "source_inventory_sha256"}
SOURCE_KEYS = {"kind", "identity_sha256", "provider_manifest_sha256"}
SOURCE_KINDS = {"clean-rocky-oci", "locked-sysroot"}
PROVIDER_MANIFEST_KEYS = {"$schema", "schema_version", "kind", "targets"}
PROVIDER_TARGET_KEYS = {"arch", "triple", "providers"}
PROVIDER_KEYS = {"soname", "path"}
EXPORT_KEYS = {"name", "version"}
INVENTORY_PROVIDER_KEYS = {
    "path",
    "soname",
    "sha256",
    "exports",
    "unversioned_exports",
    "nonpublic_versioned_exports",
}
NONPUBLIC_EXPORT_KEYS = {"name", "version", "classification"}
ELF_PROFILE_KEYS = {
    "textrel",
    "relr",
    "rpath",
    "runpath",
    "gnu_stack",
    "writable_executable_segments",
    "interpreter",
    "relro",
    "bind_now",
}
ELF_POLICY_KEYS = {"profiles", "artifact_exceptions"}
ELF_PROFILE_NAMES = {
    "compiler-default-observation",
    "crossforge-qualified-v1",
}
ELF_KINDS = {
    "dynamic-executable",
    "shared-object",
    "relocatable",
    "static-executable",
}
INTERPRETER_RULE_KEYS = {"decision", "expected"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")
SONAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")
SYMBOL_RE = re.compile(r"^[^@\s]+\Z")
VERSION_RE = re.compile(
    r"^(GLIBCXX|GLIBC|CXXABI|GCC|XCRYPT)_([A-Za-z0-9][A-Za-z0-9_.-]*)\Z"
)
SYMBOL_LINE_RE = re.compile(r"^\s*[0-9]+:\s+")
VERSION_SUFFIX_RE = re.compile(r"\s+\(([0-9]+)\)\s*\Z")
TARGETS = {
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "readelf_machine": "Advanced Micro Devices X86-64",
        "elf_class": "ELF64",
        "elf_data": "2's complement, little endian",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
    },
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "readelf_machine": "AArch64",
        "elf_class": "ELF64",
        "elf_data": "2's complement, little endian",
        "interpreter": "/lib/ld-linux-aarch64.so.1",
    },
}
RELEASE_ABI_FILES = {
    "provider_manifest": "config/abi-providers.json",
    "x86_64_baseline": "abi/el8/x86_64.json",
    "aarch64_baseline": "abi/el8/aarch64.json",
    "x86_64_sysroot_inventory": (
        "evidence/abi/el8-x86_64-sysroot.json"
    ),
    "aarch64_sysroot_inventory": (
        "evidence/abi/el8-aarch64-sysroot.json"
    ),
    "python_runtime_provider_policy": (
        "config/python-runtime-providers.json"
    ),
    "x86_64_python_provider_catalog": (
        "evidence/abi/el8-x86_64-python-provider-catalog.json"
    ),
    "aarch64_python_provider_catalog": (
        "evidence/abi/el8-aarch64-python-provider-catalog.json"
    ),
}
PUBLIC_NAMESPACES = ("GLIBC", "GLIBCXX", "CXXABI", "GCC", "XCRYPT")
PROVIDER_SCOPED_PUBLIC_NAMESPACES = {
    "XCRYPT": frozenset(("libcrypt.so.1",)),
}
UNVERSIONED_DISPOSITION = "recorded-not-allowlisted"
EXPECTED_PROVIDERS = {
    "aarch64": (
        ("ld-linux-aarch64.so.1", "usr/lib64/ld-linux-aarch64.so.1"),
        ("libBrokenLocale.so.1", "usr/lib64/libBrokenLocale.so.1"),
        ("libanl.so.1", "usr/lib64/libanl.so.1"),
        ("libc.so.6", "usr/lib64/libc.so.6"),
        ("libcrypt.so.1", "usr/lib64/libcrypt.so.1"),
        ("libdl.so.2", "usr/lib64/libdl.so.2"),
        ("libgcc_s.so.1", "usr/lib64/libgcc_s.so.1"),
        ("libm.so.6", "usr/lib64/libm.so.6"),
        ("libpthread.so.0", "usr/lib64/libpthread.so.0"),
        ("libresolv.so.2", "usr/lib64/libresolv.so.2"),
        ("librt.so.1", "usr/lib64/librt.so.1"),
        ("libstdc++.so.6", "usr/lib64/libstdc++.so.6"),
        ("libthread_db.so.1", "usr/lib64/libthread_db.so.1"),
        ("libutil.so.1", "usr/lib64/libutil.so.1"),
    ),
    "x86_64": (
        ("ld-linux-x86-64.so.2", "usr/lib64/ld-linux-x86-64.so.2"),
        ("libBrokenLocale.so.1", "usr/lib64/libBrokenLocale.so.1"),
        ("libanl.so.1", "usr/lib64/libanl.so.1"),
        ("libc.so.6", "usr/lib64/libc.so.6"),
        ("libcrypt.so.1", "usr/lib64/libcrypt.so.1"),
        ("libdl.so.2", "usr/lib64/libdl.so.2"),
        ("libgcc_s.so.1", "usr/lib64/libgcc_s.so.1"),
        ("libm.so.6", "usr/lib64/libm.so.6"),
        ("libmvec.so.1", "usr/lib64/libmvec.so.1"),
        ("libpthread.so.0", "usr/lib64/libpthread.so.0"),
        ("libresolv.so.2", "usr/lib64/libresolv.so.2"),
        ("librt.so.1", "usr/lib64/librt.so.1"),
        ("libstdc++.so.6", "usr/lib64/libstdc++.so.6"),
        ("libthread_db.so.1", "usr/lib64/libthread_db.so.1"),
        ("libutil.so.1", "usr/lib64/libutil.so.1"),
    ),
}
EXPECTED_ARTIFACT_EXCEPTIONS = [
    {
        "artifact": "toolchain/catch",
        "profile": "crossforge-qualified-v1",
        "exceptions": [
            {
                "check": "runpath",
                "allowed_values": ["$ORIGIN"],
                "reason": "test-only cross-DSO lookup",
            }
        ],
    },
    {
        "artifact": "toolchain/compiler-default-canary",
        "profile": "compiler-default-observation",
        "exceptions": [],
    },
]


class AbiContractError(ValueError):
    """An ABI document or readelf claim violates the contract."""


def require(condition, message):
    if not condition:
        raise AbiContractError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AbiContractError("duplicate JSON key: %r" % key)
        result[key] = value
    return result


def reject_nonfinite_constant(value):
    raise AbiContractError("non-finite JSON number: %s" % value)


def parse_finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise AbiContractError("non-finite JSON number: %s" % value)
    return parsed


def load_json(path):
    """Load strict JSON, rejecting duplicate keys and non-finite numbers."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(
                stream,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonfinite_constant,
                parse_float=parse_finite_float,
            )
    except AbiContractError:
        raise
    except (OSError, ValueError) as error:
        raise AbiContractError("%s: %s" % (path, error)) from error


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_manifest_path(value, label):
    require(type(value) is str and value, "%s must be a string" % label)
    require(not value.startswith("/"), "%s must be relative to the extraction root" % label)
    require("//" not in value, "%s contains an empty path component" % label)
    parts = value.split("/")
    require(
        all(part not in ("", ".", "..") for part in parts)
        and posixpath.normpath(value) == value,
        "%s is not a canonical safe relative path" % label,
    )
    return value


def validate_provider_manifest(document):
    """Validate the fixed, complete public provider set for both targets."""
    require(
        type(document) is dict and set(document) == PROVIDER_MANIFEST_KEYS,
        "ABI provider manifest fields differ",
    )
    require(
        document["$schema"] == PROVIDER_MANIFEST_SCHEMA_ID,
        "unsupported ABI provider manifest schema",
    )
    require(
        type(document["schema_version"]) is int
        and document["schema_version"] == 1,
        "unsupported ABI provider manifest schema version",
    )
    require(
        document["kind"] == PROVIDER_MANIFEST_KIND,
        "unsupported ABI provider manifest kind",
    )
    targets = document["targets"]
    require(type(targets) is list and len(targets) == 2, "ABI provider manifest must have two targets")
    arches = []
    for index, target in enumerate(targets):
        label = "ABI provider target %d" % index
        require(
            type(target) is dict and set(target) == PROVIDER_TARGET_KEYS,
            "%s fields differ" % label,
        )
        _validate_target(
            {"arch": target["arch"], "triple": target["triple"]}
        )
        arch = target["arch"]
        providers = target["providers"]
        require(type(providers) is list, "%s providers must be an array" % label)
        observed = []
        for provider_index, provider in enumerate(providers):
            provider_label = "%s provider %d" % (label, provider_index)
            require(
                type(provider) is dict and set(provider) == PROVIDER_KEYS,
                "%s fields differ" % provider_label,
            )
            soname = _validate_soname(provider["soname"], provider_label + " SONAME")
            path = _validate_manifest_path(provider["path"], provider_label + " path")
            observed.append((soname, path))
        require(
            tuple(observed) == EXPECTED_PROVIDERS[arch],
            "%s providers differ from the fixed public provider set" % label,
        )
        arches.append(arch)
    require(arches == ["aarch64", "x86_64"], "ABI provider targets are not sorted")
    return document


def load_provider_manifest(path, expected_sha256=None):
    document = load_json(path)
    validate_provider_manifest(document)
    if expected_sha256 is not None:
        _validate_sha256(expected_sha256, "expected provider manifest SHA256")
        require(
            canonical_sha256(document) == expected_sha256,
            "ABI provider manifest canonical SHA256 differs",
        )
    return document


def provider_manifest_target(document, arch, triple):
    validate_provider_manifest(document)
    matches = [
        target
        for target in document["targets"]
        if target["arch"] == arch and target["triple"] == triple
    ]
    require(len(matches) == 1, "ABI provider manifest has no unique requested target")
    return matches[0]


def _validate_sha256(value, label):
    require(
        type(value) is str and SHA256_RE.match(value),
        "%s must be 64 lowercase hexadecimal characters" % label,
    )
    return value


def validate_release_abi_identities(release):
    """Validate the fixed logical paths and digests in release.json."""
    require(type(release) is dict, "release configuration must be an object")
    abi = release.get("abi")
    require(
        type(abi) is dict
        and set(abi) == {"provider_manifest", "targets", "python"},
        "release ABI identity fields differ",
    )

    def identity(value, expected_file, label):
        require(
            type(value) is dict
            and set(value) == {"file", "canonical_sha256"},
            "%s fields differ" % label,
        )
        require(value["file"] == expected_file, "%s file differs" % label)
        _validate_sha256(value["canonical_sha256"], label + " digest")

    identity(
        abi["provider_manifest"],
        RELEASE_ABI_FILES["provider_manifest"],
        "release ABI provider manifest",
    )
    targets = abi["targets"]
    require(
        type(targets) is dict and set(targets) == set(TARGETS),
        "release ABI target identities differ",
    )
    for arch in ("x86_64", "aarch64"):
        target = targets[arch]
        require(
            type(target) is dict
            and set(target) == {"baseline", "sysroot_inventory"},
            "release ABI %s target fields differ" % arch,
        )
        identity(
            target["baseline"],
            RELEASE_ABI_FILES[arch + "_baseline"],
            "release ABI %s baseline" % arch,
        )
        identity(
            target["sysroot_inventory"],
            RELEASE_ABI_FILES[arch + "_sysroot_inventory"],
            "release ABI %s sysroot inventory" % arch,
        )

    python = abi["python"]
    require(
        type(python) is dict
        and set(python)
        == {"runtime_provider_policy", "provider_catalogs"},
        "release Python ABI identity fields differ",
    )
    identity(
        python["runtime_provider_policy"],
        RELEASE_ABI_FILES["python_runtime_provider_policy"],
        "release Python runtime provider policy",
    )
    catalogs = python["provider_catalogs"]
    require(
        type(catalogs) is dict and set(catalogs) == set(TARGETS),
        "release Python provider catalog targets differ",
    )
    for arch in ("x86_64", "aarch64"):
        identity(
            catalogs[arch],
            RELEASE_ABI_FILES[arch + "_python_provider_catalog"],
            "release Python %s provider catalog" % arch,
        )
    return abi


def release_abi_inputs(release, arch):
    """Return the five canonical ABI inputs consumed for one target."""
    abi = validate_release_abi_identities(release)
    require(arch in TARGETS, "unsupported release ABI target")
    return {
        "provider_manifest": abi["provider_manifest"],
        "baseline": abi["targets"][arch]["baseline"],
        "sysroot_inventory": abi["targets"][arch][
            "sysroot_inventory"
        ],
        "runtime_provider_policy": abi["python"][
            "runtime_provider_policy"
        ],
        "provider_catalog": abi["python"]["provider_catalogs"][arch],
    }


def _validate_target(target, expected_arch=None, expected_triple=None):
    require(type(target) is dict and set(target) == TARGET_KEYS, "target fields differ")
    arch = target["arch"]
    triple = target["triple"]
    require(type(arch) is str and arch in TARGETS, "unsupported target architecture")
    require(
        type(triple) is str and triple == TARGETS[arch]["triple"],
        "target architecture and triple differ",
    )
    if expected_arch is not None:
        require(type(expected_arch) is str, "expected architecture must be a string")
        require(arch == expected_arch, "ABI target architecture differs from expected")
    if expected_triple is not None:
        require(type(expected_triple) is str, "expected triple must be a string")
        require(triple == expected_triple, "ABI target triple differs from expected")
    return target


def classify_version(version, provider=None):
    """Classify one GNU symbol-version node without numeric assumptions."""
    require(type(version) is str and version, "symbol version must be a string")
    match = VERSION_RE.match(version)
    if not match:
        return "unknown-namespace"
    namespace = match.group(1)
    suffix = match.group(2)
    if suffix == "PRIVATE" or suffix.startswith("PRIVATE_"):
        return "private"
    if namespace == "GLIBC" and suffix.startswith("ABI_"):
        return "abi-internal"
    allowed_providers = PROVIDER_SCOPED_PUBLIC_NAMESPACES.get(namespace)
    if allowed_providers is not None and provider not in allowed_providers:
        return "unknown-namespace"
    return "public"


def _validate_symbol_name(value, label):
    require(
        type(value) is str and SYMBOL_RE.match(value),
        "%s is not a canonical symbol name" % label,
    )
    return value


def _validate_public_export(record, label, provider):
    require(type(record) is dict and set(record) == EXPORT_KEYS, "%s fields differ" % label)
    _validate_symbol_name(record["name"], label + " name")
    version = record["version"]
    classification = classify_version(version, provider)
    require(
        classification == "public",
        "%s uses a %s version node: %r" % (label, classification, version),
    )
    return record


def _export_key(record):
    return (record["name"], record["version"])


def _nonpublic_export_key(record):
    return (record["name"], record["version"], record["classification"])


def _validate_exports(exports, label, require_nonempty, provider):
    require(type(exports) is list, "%s must be an array" % label)
    if require_nonempty:
        require(exports, "%s must not be empty" % label)
    for index, record in enumerate(exports):
        _validate_public_export(record, "%s[%d]" % (label, index), provider)
    keys = [_export_key(record) for record in exports]
    require(keys == sorted(keys), "%s are not sorted" % label)
    require(len(keys) == len(set(keys)), "%s contain duplicates" % label)
    return exports


def _validate_soname(value, label):
    require(
        type(value) is str and SONAME_RE.match(value),
        "%s is not a canonical SONAME" % label,
    )
    return value


def _validate_artifact(value, label):
    require(
        type(value) is str
        and value
        and not any(character.isspace() for character in value),
        "%s must be a nonempty identifier without whitespace" % label,
    )
    return value


def _validate_profile(profile, label):
    require(
        type(profile) is dict and set(profile) == ELF_PROFILE_KEYS,
        "%s fields differ; every ELF decision must be explicit" % label,
    )
    for check in ("textrel", "relr", "rpath", "runpath", "writable_executable_segments"):
        require(profile[check] in ("forbid", "allow"), "%s %s decision is invalid" % (label, check))
    require(
        profile["gnu_stack"] in ("require-non-executable", "allow-executable"),
        "%s GNU_STACK decision is invalid" % label,
    )
    interpreter = profile["interpreter"]
    require(
        type(interpreter) is dict and set(interpreter) == INTERPRETER_RULE_KEYS,
        "%s interpreter fields differ" % label,
    )
    require(interpreter["decision"] == "require", "%s interpreter decision is invalid" % label)
    expected = interpreter["expected"]
    require(
        type(expected) is str
        and expected.startswith("/")
        and posixpath.normpath(expected) == expected,
        "%s interpreter must be an absolute canonical path" % label,
    )
    for check in ("relro", "bind_now"):
        require(
            profile[check] in ("require", "require-absent"),
            "%s %s decision is invalid" % (label, check),
        )


def _validate_profile_contract(name, profile):
    expected = {
        "textrel": "forbid",
        "relr": "forbid",
        "rpath": "forbid",
        "runpath": "forbid",
        "gnu_stack": "require-non-executable",
        "writable_executable_segments": "forbid",
        "relro": "require",
        "bind_now": (
            "require-absent"
            if name == "compiler-default-observation"
            else "require"
        ),
    }
    for check, decision in expected.items():
        require(
            profile[check] == decision,
            "%s must set %s to %s" % (name, check, decision),
        )


def _validate_elf_policy(policy, target):
    require(
        type(policy) is dict and set(policy) == ELF_POLICY_KEYS,
        "ELF policy fields differ",
    )
    profiles = policy["profiles"]
    require(
        type(profiles) is dict and set(profiles) == ELF_PROFILE_NAMES,
        "ELF policy must define both fixed profiles exactly",
    )
    interpreters = set()
    for name, profile in profiles.items():
        _validate_profile(profile, "ELF profile %s" % name)
        _validate_profile_contract(name, profile)
        interpreters.add(profile["interpreter"]["expected"])
    require(len(interpreters) == 1, "ELF profiles disagree on interpreter")
    require(
        interpreters == {TARGETS[target["arch"]]["interpreter"]},
        "ELF profile interpreter differs from target architecture",
    )
    overrides = policy["artifact_exceptions"]
    require(
        overrides == EXPECTED_ARTIFACT_EXCEPTIONS,
        "ELF artifact exceptions differ from the two fixed v1 records",
    )


def validate_baseline(document, expected_arch=None, expected_triple=None):
    """Validate a reviewed baseline against optional trusted target inputs."""
    require(type(document) is dict and set(document) == BASELINE_KEYS, "ABI baseline fields differ")
    require(document["$schema"] == BASELINE_SCHEMA_ID, "unsupported ABI baseline schema")
    require(
        type(document["schema_version"]) is int and document["schema_version"] == 1,
        "unsupported ABI baseline schema version",
    )
    require(document["kind"] == BASELINE_KIND, "unsupported ABI baseline kind")
    require(
        type(document["baseline"]) is str and SAFE_ID_RE.match(document["baseline"]),
        "ABI baseline id is invalid",
    )
    _validate_target(document["target"], expected_arch, expected_triple)
    review = document["review"]
    require(type(review) is dict and set(review) == REVIEW_KEYS, "ABI review fields differ")
    require(review["status"] == "reviewed", "ABI baseline has not been reviewed")
    expected_inventory_path = "evidence/abi/%s-%s-clean.json" % (
        document["baseline"],
        document["target"]["arch"],
    )
    require(
        review["source_inventory"] == expected_inventory_path,
        "ABI review source inventory path differs from baseline identity",
    )
    _validate_sha256(review["source_inventory_sha256"], "source inventory SHA256")
    providers = document["providers"]
    require(type(providers) is dict and providers, "ABI baseline providers must be a nonempty object")
    for provider, exports in providers.items():
        _validate_soname(provider, "ABI baseline provider")
        _validate_exports(
            exports,
            "ABI baseline provider %s exports" % provider,
            True,
            provider,
        )
    _validate_elf_policy(document["elf_policy"], document["target"])
    return document


def load_baseline(path, expected_arch=None, expected_triple=None, expected_sha256=None):
    document = load_json(path)
    validate_baseline(document, expected_arch, expected_triple)
    if expected_sha256 is not None:
        _validate_sha256(expected_sha256, "expected ABI baseline SHA256")
        require(
            canonical_sha256(document) == expected_sha256,
            "ABI baseline canonical SHA256 differs",
        )
    return document


def _validate_inventory_provider(provider_key, provider):
    label = "ABI inventory provider %s" % provider_key
    require(
        type(provider) is dict and set(provider) == INVENTORY_PROVIDER_KEYS,
        "%s fields differ" % label,
    )
    _validate_soname(provider["soname"], label + " SONAME")
    require(provider["soname"] == provider_key, "%s key and SONAME differ" % label)
    path = provider["path"]
    require(
        type(path) is str
        and path.startswith("/")
        and posixpath.normpath(path) == path,
        "%s path is not absolute and canonical" % label,
    )
    _validate_sha256(provider["sha256"], label + " SHA256")
    _validate_exports(provider["exports"], label + " exports", False, provider_key)
    unversioned = provider["unversioned_exports"]
    require(type(unversioned) is list, "%s unversioned_exports must be an array" % label)
    for index, name in enumerate(unversioned):
        _validate_symbol_name(name, "%s unversioned_exports[%d]" % (label, index))
    require(unversioned == sorted(unversioned), "%s unversioned_exports are not sorted" % label)
    require(len(unversioned) == len(set(unversioned)), "%s unversioned_exports repeat" % label)
    nonpublic = provider["nonpublic_versioned_exports"]
    require(type(nonpublic) is list, "%s nonpublic exports must be an array" % label)
    for index, record in enumerate(nonpublic):
        item_label = "%s nonpublic_versioned_exports[%d]" % (label, index)
        require(
            type(record) is dict and set(record) == NONPUBLIC_EXPORT_KEYS,
            "%s fields differ" % item_label,
        )
        _validate_symbol_name(record["name"], item_label + " name")
        classification = classify_version(record["version"], provider_key)
        require(classification != "public", "%s contains a public version" % item_label)
        require(
            record["classification"] == classification,
            "%s classification differs from its version node" % item_label,
        )
    nonpublic_keys = [_nonpublic_export_key(record) for record in nonpublic]
    require(nonpublic_keys == sorted(nonpublic_keys), "%s nonpublic exports are not sorted" % label)
    require(len(nonpublic_keys) == len(set(nonpublic_keys)), "%s nonpublic exports repeat" % label)


def validate_inventory(document, expected_arch=None, expected_triple=None):
    """Validate an extraction inventory; this does not approve an allowlist."""
    require(type(document) is dict and set(document) == INVENTORY_KEYS, "ABI inventory fields differ")
    require(document["$schema"] == INVENTORY_SCHEMA_ID, "unsupported ABI inventory schema")
    require(
        type(document["schema_version"]) is int and document["schema_version"] == 1,
        "unsupported ABI inventory schema version",
    )
    require(document["kind"] == INVENTORY_KIND, "unsupported ABI inventory kind")
    _validate_target(document["target"], expected_arch, expected_triple)
    source = document["source"]
    require(type(source) is dict and set(source) == SOURCE_KEYS, "ABI inventory source fields differ")
    require(source["kind"] in SOURCE_KINDS, "ABI inventory source kind is invalid")
    _validate_sha256(source["identity_sha256"], "ABI inventory source identity SHA256")
    _validate_sha256(
        source["provider_manifest_sha256"],
        "ABI inventory provider manifest SHA256",
    )
    providers = document["providers"]
    require(type(providers) is dict and providers, "ABI inventory providers must be a nonempty object")
    paths = []
    for provider_key, provider in providers.items():
        _validate_soname(provider_key, "ABI inventory provider key")
        _validate_inventory_provider(provider_key, provider)
        paths.append(provider["path"])
    require(len(paths) == len(set(paths)), "ABI inventory repeats a provider path")
    expected = {
        soname: "/" + path
        for soname, path in EXPECTED_PROVIDERS[document["target"]["arch"]]
    }
    observed = {
        soname: provider["path"] for soname, provider in providers.items()
    }
    require(
        observed == expected,
        "ABI inventory providers differ from the fixed provider manifest",
    )
    return document


def load_inventory(path, expected_arch=None, expected_triple=None, expected_sha256=None):
    document = load_json(path)
    validate_inventory(document, expected_arch, expected_triple)
    if expected_sha256 is not None:
        _validate_sha256(expected_sha256, "expected ABI inventory SHA256")
        require(
            canonical_sha256(document) == expected_sha256,
            "ABI inventory canonical SHA256 differs",
        )
    return document


def validate_inventory_provider_manifest(inventory, provider_manifest):
    """Bind inventory membership to the reviewed fixed provider manifest."""
    validate_inventory(inventory)
    validate_provider_manifest(provider_manifest)
    require(
        inventory["source"]["provider_manifest_sha256"]
        == canonical_sha256(provider_manifest),
        "inventory provider manifest SHA256 differs",
    )
    target = provider_manifest_target(
        provider_manifest,
        inventory["target"]["arch"],
        inventory["target"]["triple"],
    )
    expected = sorted(
        (provider["soname"], "/" + provider["path"])
        for provider in target["providers"]
    )
    observed = sorted(
        (soname, details["path"])
        for soname, details in inventory["providers"].items()
    )
    require(
        observed == expected,
        "inventory providers differ from the fixed provider manifest",
    )
    return inventory


def _inventory_export_sets(inventory):
    return {
        provider: {_export_key(record) for record in details["exports"]}
        for provider, details in inventory["providers"].items()
    }


def _export_records(values):
    return [
        {"name": name, "version": version}
        for name, version in sorted(values)
    ]


def _inventory_diff(inventory, baseline):
    inventory_exports = _inventory_export_sets(inventory)
    baseline_exports = {
        provider: {_export_key(record) for record in exports}
        for provider, exports in baseline["providers"].items()
    }
    missing_providers = sorted(set(baseline_exports) - set(inventory_exports))
    extra_providers = sorted(set(inventory_exports) - set(baseline_exports))
    missing_exports = {}
    extra_exports = {}
    for provider in sorted(set(baseline_exports).intersection(inventory_exports)):
        missing = baseline_exports[provider] - inventory_exports[provider]
        extra = inventory_exports[provider] - baseline_exports[provider]
        if missing:
            missing_exports[provider] = _export_records(missing)
        if extra:
            extra_exports[provider] = _export_records(extra)
    for provider in extra_providers:
        extra_exports[provider] = _export_records(inventory_exports[provider])
    return {
        "missing_providers": missing_providers,
        "extra_providers": extra_providers,
        "missing_exports": missing_exports,
        "extra_exports": extra_exports,
    }


def validate_baseline_against_inventory(baseline, clean_inventory, require_exact=True):
    """Bind a reviewed floor to its clean-root provider inventory.

    Exact mode is the reproducible initial-floor boundary.  It rejects both
    missing and additional public exports; no candidate symbol is promoted by
    this function.
    """
    require(type(require_exact) is bool, "require_exact must be a boolean")
    validate_baseline(baseline)
    validate_inventory(clean_inventory)
    require(
        baseline["target"] == clean_inventory["target"],
        "ABI baseline and clean inventory targets differ",
    )
    require(
        clean_inventory["source"]["kind"] == "clean-rocky-oci",
        "baseline source inventory is not a clean Rocky OCI inventory",
    )
    require(
        baseline["review"]["source_inventory_sha256"]
        == canonical_sha256(clean_inventory),
        "baseline review digest differs from clean inventory",
    )
    difference = _inventory_diff(clean_inventory, baseline)
    require(not difference["missing_providers"], "clean inventory is missing a baseline provider")
    require(not difference["missing_exports"], "clean inventory is missing a baseline export")
    if require_exact:
        require(not difference["extra_providers"], "clean inventory has an unreviewed provider")
        require(not difference["extra_exports"], "clean inventory has an unreviewed public export")
    return difference


def validate_inventory_superset(sysroot_inventory, baseline):
    """Validate errata inventory as a superset and return every explicit extra.

    Additional providers or same-version symbols are reported in
    ``extra_exports`` and remain outside the reviewed ABI baseline.
    """
    validate_inventory(sysroot_inventory)
    validate_baseline(baseline)
    require(
        sysroot_inventory["target"] == baseline["target"],
        "sysroot inventory and ABI baseline targets differ",
    )
    require(
        sysroot_inventory["source"]["kind"] == "locked-sysroot",
        "ABI superset inventory is not bound to a locked sysroot",
    )
    difference = _inventory_diff(sysroot_inventory, baseline)
    require(not difference["missing_providers"], "sysroot inventory is missing a baseline provider")
    require(not difference["missing_exports"], "sysroot inventory is missing a baseline export")
    return difference


def _symbol_rows(dynamic_symbols):
    require(type(dynamic_symbols) is str, "readelf dynamic-symbol output must be text")
    rows = []
    for raw_line in dynamic_symbols.splitlines():
        if not SYMBOL_LINE_RE.match(raw_line):
            continue
        fields = raw_line.split(None, 7)
        require(len(fields) >= 7, "malformed readelf dynamic-symbol row")
        if len(fields) == 7:
            # GNU readelf prints the mandatory null dynsym entry without a name.
            continue
        raw_name = fields[7].strip()
        suffix = VERSION_SUFFIX_RE.search(raw_name)
        version_index = suffix.group(1) if suffix else None
        name = VERSION_SUFFIX_RE.sub("", raw_name)
        if not name:
            continue
        rows.append(
            {
                "type": fields[3],
                "binding": fields[4],
                "visibility": fields[5],
                "index": fields[6],
                "name": name,
                "version_index": version_index,
            }
        )
    return rows


def _split_symbol_version(name):
    if "@@" in name:
        symbol, version = name.rsplit("@@", 1)
    elif "@" in name:
        symbol, version = name.rsplit("@", 1)
    else:
        return name, None
    require(symbol and version, "malformed versioned symbol: %r" % name)
    return symbol, version


def parse_readelf_version_needs(version_info):
    """Return numeric version indexes bound to provider and version node."""
    require(type(version_info) is str, "readelf version-info output must be text")
    in_needs = False
    provider = None
    needs = {}
    for raw_line in version_info.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("Version needs section"):
            in_needs = True
            provider = None
            continue
        if stripped.startswith("Version ") and not stripped.startswith("Version: "):
            in_needs = False
            provider = None
            continue
        if not in_needs:
            continue
        file_match = re.search(r"\bFile:\s+(\S+)", raw_line)
        if file_match:
            provider = file_match.group(1)
            _validate_soname(provider, "readelf version provider")
            continue
        name_match = re.search(r"\bName:\s+(\S+)", raw_line)
        if name_match:
            require(provider is not None, "readelf version need has no provider")
            version = name_match.group(1)
            index_match = re.search(r"\bVersion:\s+([0-9]+)\s*$", raw_line)
            require(index_match is not None, "readelf version need has no numeric index")
            index = index_match.group(1)
            record = {"provider": provider, "version": version}
            previous = needs.get(index)
            require(
                previous is None or previous == record,
                "readelf repeats version index %s with different requirements" % index,
            )
            needs[index] = record
    return needs


def parse_readelf_imports(dynamic_symbols):
    """Separate exact versioned requirements from unversioned imports."""
    versioned = set()
    unversioned = set()
    for row in _symbol_rows(dynamic_symbols):
        if row["index"] != "UND":
            continue
        name, version = _split_symbol_version(row["name"])
        _validate_symbol_name(name, "undefined symbol")
        if version is None:
            unversioned.add(name)
        else:
            require(
                row["version_index"] is not None,
                "versioned undefined symbol has no numeric version index",
            )
            versioned.add((name, version, row["version_index"]))
    return {
        "versioned": [
            {"name": name, "version": version, "version_index": version_index}
            for name, version, version_index in sorted(versioned)
        ],
        "unversioned": sorted(unversioned),
    }


def provider_inventory_from_readelf(path, soname, sha256, dynamic_symbols):
    """Build one provider inventory record from its byte identity and dynsym."""
    _validate_soname(soname, "provider SONAME")
    _validate_sha256(sha256, "provider SHA256")
    require(
        type(path) is str and path.startswith("/") and posixpath.normpath(path) == path,
        "provider path is not absolute and canonical",
    )
    public = set()
    unversioned = set()
    nonpublic = set()
    for row in _symbol_rows(dynamic_symbols):
        if row["index"] == "UND":
            continue
        if row["binding"] not in ("GLOBAL", "WEAK", "UNIQUE"):
            continue
        if row["visibility"] not in ("DEFAULT", "PROTECTED"):
            continue
        name, version = _split_symbol_version(row["name"])
        _validate_symbol_name(name, "provider export")
        if version is None:
            unversioned.add(name)
            continue
        if name == version and row["index"] == "ABS":
            continue
        classification = classify_version(version, soname)
        if classification == "public":
            public.add((name, version))
        else:
            nonpublic.add((name, version, classification))
    record = {
        "path": path,
        "soname": soname,
        "sha256": sha256,
        "exports": [
            {"name": name, "version": version}
            for name, version in sorted(public)
        ],
        "unversioned_exports": sorted(unversioned),
        "nonpublic_versioned_exports": [
            {"name": name, "version": version, "classification": classification}
            for name, version, classification in sorted(nonpublic)
        ],
    }
    _validate_inventory_provider(soname, record)
    return record


def audit_symbol_requirements(baseline, dynamic_symbols, version_info):
    """Audit undefined symbols against exact provider/name/version triples.

    Unversioned imports are reported but intentionally receive no passing ABI
    verdict.  They must be evaluated later against explicit dependency
    ownership; no API in this module can add them to ``baseline.providers``.
    """
    validate_baseline(baseline)
    imports = parse_readelf_imports(dynamic_symbols)
    needs = parse_readelf_version_needs(version_info)
    allowed = {
        provider: {_export_key(record) for record in exports}
        for provider, exports in baseline["providers"].items()
    }
    versioned = []
    for record in imports["versioned"]:
        name = record["name"]
        version = record["version"]
        version_index = record["version_index"]
        require(
            version_index in needs,
            "required version index %s has no readelf provider" % version_index,
        )
        need = needs[version_index]
        require(
            need["version"] == version,
            "dynamic symbol version differs from its readelf version need",
        )
        provider = need["provider"]
        classification = classify_version(version, provider)
        require(
            classification == "public",
            "required symbol %s uses a %s version node %s"
            % (name, classification, version),
        )
        require(provider in allowed, "required ABI provider is not allowlisted: %s" % provider)
        require(
            (name, version) in allowed[provider],
            "required ABI export is not allowlisted: %s:%s@%s"
            % (provider, name, version),
        )
        versioned.append({"provider": provider, "name": name, "version": version})
    versioned.sort(key=lambda item: (item["provider"], item["name"], item["version"]))
    return {
        "versioned_imports": versioned,
        "unversioned_imports": {
            "disposition": UNVERSIONED_DISPOSITION,
            "symbols": imports["unversioned"],
        },
    }


def _dynamic_properties(dynamic_section):
    require(type(dynamic_section) is str, "readelf dynamic-section output must be text")
    reject_load_affecting_dynamic_tags(dynamic_section)
    paths = {"rpath": [], "runpath": []}
    seen_path_tags = set()
    path_tags = []
    for match in re.finditer(r"\((RPATH|RUNPATH)\).*?\[([^\]]*)\]", dynamic_section):
        key = match.group(1).lower()
        require(key not in seen_path_tags, "readelf reports multiple %s tags" % match.group(1))
        raw_value = match.group(2)
        components = raw_value.split(":")
        require(
            raw_value and all(component for component in components),
            "%s contains an empty path component" % match.group(1),
        )
        require(
            len(components) == len(set(components)),
            "%s contains a duplicate path component" % match.group(1),
        )
        seen_path_tags.add(key)
        paths[key] = components
        path_tags.append({"tag": match.group(1), "components": list(components)})
    return {
        "dynamic_section": "no dynamic section" not in dynamic_section.lower(),
        "textrel": bool(re.search(r"\(TEXTREL\)|\bTEXTREL\b", dynamic_section)),
        "relr": bool(re.search(r"\((?:RELR|RELRSZ|RELRENT)\)", dynamic_section)),
        "rpath": paths["rpath"],
        "runpath": paths["runpath"],
        "path_tags": path_tags,
        "pie": bool(
            re.search(r"\(FLAGS_1\).*\bPIE\b", dynamic_section)
        ),
        "bind_now": bool(
            re.search(r"\(BIND_NOW\)", dynamic_section)
            or re.search(r"\((?:FLAGS|FLAGS_1)\).*\b(?:BIND_NOW|NOW)\b", dynamic_section)
        ),
    }


def reject_load_affecting_dynamic_tags(dynamic_section, allow_symbolic=False):
    """Reject dynamic-loader injection and lookup-order overrides globally."""
    require(type(dynamic_section) is str, "readelf dynamic-section output must be text")
    tags = sorted(
        set(
            re.findall(
                r"\((AUDIT|DEPAUDIT|FILTER|AUXILIARY)\)",
                dynamic_section,
            )
        )
    )
    require(type(allow_symbolic) is bool, "allow_symbolic must be a boolean")
    if not allow_symbolic:
        if re.search(r"\(SYMBOLIC\)", dynamic_section):
            tags.append("SYMBOLIC")
        if re.search(
            r"\((?:FLAGS|FLAGS_1)\).*\bSYMBOLIC\b",
            dynamic_section,
        ):
            tags.append("SYMBOLIC-FLAG")
    require(
        not tags,
        "load-affecting dynamic tags are forbidden: %s"
        % ", ".join(sorted(set(tags))),
    )
    return []


def _program_header_properties(program_headers):
    require(type(program_headers) is str, "readelf program-header output must be text")
    interpreter_match = re.search(
        r"\[Requesting program interpreter:\s*([^\]]+)\]", program_headers
    )
    stack_flags = []
    wx_segments = False
    relro = False
    for raw_line in program_headers.splitlines():
        fields = raw_line.split()
        if not fields:
            continue
        segment = fields[0]
        if segment == "GNU_RELRO":
            relro = True
        if segment not in ("LOAD", "GNU_STACK") or len(fields) < 8:
            continue
        flags = "".join(fields[6:-1])
        if segment == "GNU_STACK":
            stack_flags.append(flags)
        elif "W" in flags and "E" in flags:
            wx_segments = True
    require(len(stack_flags) <= 1, "readelf reports multiple GNU_STACK headers")
    return {
        "interpreter": interpreter_match.group(1) if interpreter_match else None,
        "gnu_stack_present": bool(stack_flags),
        "gnu_stack_executable": bool(stack_flags and "E" in stack_flags[0]),
        "writable_executable_segments": wx_segments,
        "relro": relro,
    }


def _elf_header_properties(elf_header):
    require(type(elf_header) is str, "readelf ELF-header output must be text")
    machine = re.search(r"^\s*Machine:\s*(.*?)\s*$", elf_header, re.MULTILINE)
    elf_class = re.search(r"^\s*Class:\s*(.*?)\s*$", elf_header, re.MULTILINE)
    elf_data = re.search(r"^\s*Data:\s*(.*?)\s*$", elf_header, re.MULTILINE)
    elf_type = re.search(r"^\s*Type:\s*([A-Z]+)\b", elf_header, re.MULTILINE)
    require(
        machine is not None
        and machine.group(1)
        and elf_class is not None
        and elf_class.group(1)
        and elf_data is not None
        and elf_data.group(1),
        "readelf ELF header omits class, data encoding, or machine",
    )
    require(elf_type is not None, "readelf ELF header has no type")
    return {
        "machine": machine.group(1),
        "elf_class": elf_class.group(1),
        "elf_data": elf_data.group(1),
        "elf_type": elf_type.group(1),
    }


def parse_elf_properties(dynamic_section, program_headers, elf_header):
    properties = _dynamic_properties(dynamic_section)
    properties.update(_program_header_properties(program_headers))
    properties.update(_elf_header_properties(elf_header))
    return properties


def _artifact_override(policy, artifact):
    matches = [
        rule
        for rule in policy["artifact_exceptions"]
        if rule["artifact"] == artifact
    ]
    require(len(matches) <= 1, "artifact has multiple ELF policy overrides: %s" % artifact)
    return matches[0] if matches else None


def _artifact_exception(override, check):
    if override is None:
        return None
    matches = [exception for exception in override["exceptions"] if exception["check"] == check]
    require(len(matches) <= 1, "artifact repeats an ELF exception")
    return matches[0] if matches else None


def _infer_elf_kind(properties):
    if properties["elf_type"] == "REL" and not properties["dynamic_section"]:
        return "relocatable"
    if (
        properties["elf_type"] == "EXEC"
        and not properties["dynamic_section"]
        and properties["interpreter"] is None
    ):
        return "static-executable"
    if (
        properties["elf_type"] in ("DYN", "EXEC")
        and properties["dynamic_section"]
        and properties["interpreter"] is not None
    ):
        return "dynamic-executable"
    if (
        properties["elf_type"] == "DYN"
        and properties["dynamic_section"]
        and properties["interpreter"] is None
    ):
        return "shared-object"
    raise AbiContractError("readelf evidence does not identify a supported ELF kind")


def audit_elf_policy(
    baseline,
    artifact,
    dynamic_section,
    program_headers,
    elf_header,
    profile_name="crossforge-qualified-v1",
):
    """Apply the artifact's explicit named profile without implicit defaults."""
    validate_baseline(baseline)
    _validate_artifact(artifact, "artifact")
    properties = parse_elf_properties(dynamic_section, program_headers, elf_header)
    target = baseline["target"]
    require(
        properties["machine"] == TARGETS[target["arch"]]["readelf_machine"],
        "ELF machine differs from ABI baseline target",
    )
    require(
        properties["elf_class"] == TARGETS[target["arch"]]["elf_class"]
        and properties["elf_data"] == TARGETS[target["arch"]]["elf_data"],
        "ELF class or data encoding differs from ABI baseline target",
    )
    policy = baseline["elf_policy"]
    require(profile_name in ELF_PROFILE_NAMES, "requested ELF profile is invalid")
    override = _artifact_override(policy, artifact)
    if profile_name == "compiler-default-observation":
        require(
            override is not None,
            "compiler-default observation requires an exact artifact override",
        )
    if override is not None:
        require(
            profile_name == override["profile"],
            "requested ELF profile differs from the artifact override",
        )
    elf_kind = _infer_elf_kind(properties)
    require(
        elf_kind != "shared-object" or not properties["pie"],
        "PIE dynamic flag is forbidden for shared objects",
    )
    profile = policy["profiles"][profile_name]
    not_applicable = {
        "dynamic-executable": set(),
        "shared-object": {"interpreter"},
        "relocatable": {"bind_now", "gnu_stack", "interpreter", "relro"},
        "static-executable": {"bind_now", "interpreter", "relro"},
    }[elf_kind]
    used_exceptions = set()
    for label, observed in (
        ("textrel", properties["textrel"]),
        ("relr", properties["relr"]),
        ("writable_executable_segments", properties["writable_executable_segments"]),
    ):
        mismatch = profile[label] == "forbid" and observed
        if mismatch:
            require(_artifact_exception(override, label) is not None, "%s is forbidden for %s" % (label, artifact))
            used_exceptions.add(label)
    for label in ("rpath", "runpath"):
        observed = properties[label]
        if profile[label] == "forbid" and observed:
            exception = _artifact_exception(override, label)
            require(exception is not None, "%s is forbidden for %s" % (label, artifact))
            require(
                observed == exception["allowed_values"],
                "%s differs from its exact exception for %s" % (label, artifact),
            )
            used_exceptions.add(label)
    if (
        "gnu_stack" not in not_applicable
        and profile["gnu_stack"] == "require-non-executable"
    ):
        stack_ok = properties["gnu_stack_present"] and not properties["gnu_stack_executable"]
        stack_exception = _artifact_exception(override, "gnu_stack")
        require(stack_ok or stack_exception is not None, "non-executable GNU_STACK is required for %s" % artifact)
        if not stack_ok:
            used_exceptions.add("gnu_stack")
    if "interpreter" not in not_applicable:
        expected_interpreter = profile["interpreter"]["expected"]
        interpreter_ok = properties["interpreter"] == expected_interpreter
        interpreter_exception = _artifact_exception(override, "interpreter")
        require(interpreter_ok or interpreter_exception is not None, "ELF interpreter differs for %s" % artifact)
        if not interpreter_ok:
            used_exceptions.add("interpreter")
    for label in ("relro", "bind_now"):
        if label in not_applicable:
            continue
        expected = profile[label] == "require"
        mismatch = properties[label] != expected
        if mismatch:
            require(
                _artifact_exception(override, label) is not None,
                "%s decision differs for %s" % (label, artifact),
            )
            used_exceptions.add(label)
    declared_exceptions = (
        {exception["check"] for exception in override["exceptions"]}
        if override is not None
        else set()
    )
    require(
        used_exceptions == declared_exceptions,
        "ELF policy has an unused exception for %s" % artifact,
    )
    return {
        "artifact": artifact,
        "profile": profile_name,
        "elf_kind": elf_kind,
        "not_applicable": sorted(not_applicable),
        "properties": properties,
        "used_exceptions": sorted(used_exceptions),
    }


def audit_readelf(
    baseline,
    artifact,
    dynamic_symbols,
    version_info,
    dynamic_section,
    program_headers,
    elf_header,
    profile_name="crossforge-qualified-v1",
):
    """Audit exact ABI imports and all explicit ELF policy decisions."""
    symbols = audit_symbol_requirements(baseline, dynamic_symbols, version_info)
    elf = audit_elf_policy(
        baseline,
        artifact,
        dynamic_section,
        program_headers,
        elf_header,
        profile_name=profile_name,
    )
    return {
        "status": "passed",
        "artifact": artifact,
        "target": dict(baseline["target"]),
        "symbol_requirements": symbols,
        "elf": elf,
    }

#!/usr/bin/env python3
"""Strict CPython non-core runtime-provider policy primitives.

The frozen ABI baseline owns glibc, libxcrypt and the other core providers.
This module owns the separate, exact RPM-backed provider set required by
CPython extension modules.  A clean runtime is qualified only when these DSO
bytes equal the locked-sysroot bytes; this module exposes that byte comparison
without wiring it into the runtime pipeline.  The policy document is the sole
authority for DSO digests; this code fixes membership and ownership only.

Only Python 3.6-compatible standard-library interfaces are used.
"""

import hashlib
import json
import math
import os
import posixpath
import re
import runpy
import stat
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_ID = (
    "https://crossforge.dev/schemas/python-runtime-providers.schema.json"
)
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
KIND = "crossforge-python-runtime-provider-policy"
RUNTIME_CONTRACT = {
    "clean_runtime_provider_bytes": "must-equal-locked-sysroot"
}
ROOT_KEYS = {
    "$schema",
    "schema_version",
    "kind",
    "runtime_contract",
    "targets",
}
TARGET_KEYS = {
    "arch",
    "triple",
    "sysroot_lock",
    "provider_catalog_sha256",
    "owners",
    "providers",
}
SYSROOT_LOCK_KEYS = {"file", "canonical_sha256"}
PROVIDER_KEYS = {
    "soname",
    "path",
    "owner",
    "dso_sha256",
}
OWNER_KEYS = {"name", "nevra", "received_sha256"}
LOCK_KEYS = {"$schema", "schema_version", "kind", "transaction", "packages"}
LOCK_REFERENCE_KEYS = {"file", "canonical_sha256"}
LOCK_PACKAGE_KEYS = {"nevra", "received_sha256", "header", "signature"}
LOCK_HEADER_KEYS = {
    "name",
    "epoch",
    "version",
    "release",
    "arch",
    "nevra",
    "source_rpm",
}
TARGET_ORDER = ("aarch64", "x86_64")
TARGETS = {
    "aarch64": {
        "triple": "aarch64-unknown-linux-gnu",
        "lock_file": "locks/sysroot-el8-aarch64.json",
        "transaction_file": "locks/transactions/sysroot-el8-aarch64.json",
    },
    "x86_64": {
        "triple": "x86_64-unknown-linux-gnu",
        "lock_file": "locks/sysroot-el8-x86_64.json",
        "transaction_file": "locks/transactions/sysroot-el8-x86_64.json",
    },
}
EXPECTED_PROVIDERS = (
    ("libbz2.so.1", "bzip2-libs"),
    ("libcrypto.so.1.1", "openssl-libs"),
    ("libffi.so.6", "libffi"),
    ("liblzma.so.5", "xz-libs"),
    ("libsqlite3.so.0", "sqlite-libs"),
    ("libssl.so.1.1", "openssl-libs"),
    ("libuuid.so.1", "libuuid"),
    ("libz.so.1", "zlib"),
)
EXPECTED_OWNERS = (
    "bzip2-libs",
    "libffi",
    "libuuid",
    "openssl-libs",
    "sqlite-libs",
    "xz-libs",
    "zlib",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
SONAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")

ABI_CONTRACT = runpy.run_path(str(Path(__file__).with_name("abi_contract.py")))
CORE_PROVIDERS = frozenset(
    soname
    for arch in TARGET_ORDER
    for soname, _path in ABI_CONTRACT["EXPECTED_PROVIDERS"][arch]
)


class RuntimeProviderPolicyError(ValueError):
    """The Python runtime-provider policy is malformed or unbound."""


def require(condition, message):
    if not condition:
        raise RuntimeProviderPolicyError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %r" % key)
        result[key] = value
    return result


def reject_nonfinite_constant(value):
    raise RuntimeProviderPolicyError("non-finite JSON number: %s" % value)


def parse_finite_float(value):
    parsed = float(value)
    require(math.isfinite(parsed), "non-finite JSON number: %s" % value)
    return parsed


def load_json(path):
    """Load strict JSON, rejecting duplicate keys and non-finite values."""
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(
                stream,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonfinite_constant,
                parse_float=parse_finite_float,
            )
    except RuntimeProviderPolicyError:
        raise
    except (OSError, ValueError) as error:
        raise RuntimeProviderPolicyError("%s: %s" % (path, error)) from error


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _exact_fields(value, expected, label):
    require(type(value) is dict, "%s must be an object" % label)
    actual = set(value)
    require(
        actual == set(expected),
        "%s fields differ (missing=%s; extra=%s)"
        % (
            label,
            ",".join(sorted(set(expected) - actual)),
            ",".join(sorted(actual - set(expected))),
        ),
    )


def _sha256(value, label):
    require(
        type(value) is str and SHA256_RE.match(value),
        "%s must be 64 lowercase hexadecimal characters" % label,
    )
    return value


def _soname(value, label):
    require(
        type(value) is str and SONAME_RE.match(value),
        "%s is not a canonical SONAME" % label,
    )
    return value


def _logical_path(value, soname, label):
    require(type(value) is str, "%s must be a string" % label)
    require(
        value == "/usr/lib64/" + soname
        and posixpath.normpath(value) == value
        and "//" not in value,
        "%s must be the exact /usr/lib64 SONAME path" % label,
    )
    return value


def _owner(owner, expected_name, arch, label):
    _exact_fields(owner, OWNER_KEYS, label)
    name = owner["name"]
    require(
        type(name) is str and OWNER_RE.match(name),
        "%s name is invalid" % label,
    )
    require(name == expected_name, "%s name differs from fixed ownership" % label)
    nevra = owner["nevra"]
    require(
        type(nevra) is str
        and nevra
        and not any(character.isspace() for character in nevra)
        and nevra.startswith(name + "-")
        and nevra.endswith("." + arch),
        "%s NEVRA is invalid for %s" % (label, arch),
    )
    _sha256(owner["received_sha256"], label + " received_sha256")
    return owner


def validate_policy(document):
    """Validate the fixed two-target, eight-provider policy shape."""
    _exact_fields(document, ROOT_KEYS, "Python runtime provider policy")
    require(document["$schema"] == SCHEMA_ID, "unsupported provider policy schema")
    require(
        type(document["schema_version"]) is int
        and document["schema_version"] == 1,
        "unsupported provider policy schema version",
    )
    require(document["kind"] == KIND, "unsupported provider policy kind")
    require(
        document["runtime_contract"] == RUNTIME_CONTRACT,
        "provider policy runtime contract differs",
    )
    targets = document["targets"]
    require(
        type(targets) is list and len(targets) == len(TARGET_ORDER),
        "provider policy must have exactly two targets",
    )
    observed_arches = []
    for target_index, target in enumerate(targets):
        label = "provider policy target %d" % target_index
        _exact_fields(target, TARGET_KEYS, label)
        expected_arch = TARGET_ORDER[target_index]
        arch = target["arch"]
        require(arch == expected_arch, "provider policy targets are not sorted")
        expected_target = TARGETS[arch]
        require(
            target["triple"] == expected_target["triple"],
            "%s architecture and triple differ" % label,
        )
        sysroot_lock = target["sysroot_lock"]
        _exact_fields(sysroot_lock, SYSROOT_LOCK_KEYS, label + " sysroot lock")
        require(
            sysroot_lock["file"] == expected_target["lock_file"],
            "%s sysroot lock path differs" % label,
        )
        _sha256(
            sysroot_lock["canonical_sha256"],
            label + " sysroot lock canonical_sha256",
        )
        _sha256(
            target["provider_catalog_sha256"],
            label + " provider catalog SHA256",
        )
        owners = target["owners"]
        require(
            type(owners) is list and len(owners) == len(EXPECTED_OWNERS),
            "%s must have exactly seven RPM owners" % label,
        )
        owner_records = {}
        observed_owner_names = []
        for owner_index, owner in enumerate(owners):
            owner_label = "%s owner %d" % (label, owner_index)
            expected_owner = EXPECTED_OWNERS[owner_index]
            _owner(owner, expected_owner, arch, owner_label)
            require(
                owner["name"] not in owner_records,
                "%s repeats an RPM owner" % label,
            )
            owner_records[owner["name"]] = owner
            observed_owner_names.append(owner["name"])
        require(
            tuple(observed_owner_names) == EXPECTED_OWNERS,
            "%s RPM owners are not the fixed sorted set" % label,
        )
        providers = target["providers"]
        require(
            type(providers) is list
            and len(providers) == len(EXPECTED_PROVIDERS),
            "%s must have exactly eight providers" % label,
        )
        observed = []
        paths = []
        for provider_index, provider in enumerate(providers):
            provider_label = "%s provider %d" % (label, provider_index)
            _exact_fields(provider, PROVIDER_KEYS, provider_label)
            soname = _soname(provider["soname"], provider_label + " SONAME")
            expected_soname, expected_owner = EXPECTED_PROVIDERS[provider_index]
            require(
                soname == expected_soname,
                "%s providers are not the fixed sorted set" % label,
            )
            require(
                soname not in CORE_PROVIDERS,
                "%s contains core ABI provider %s" % (label, soname),
            )
            require(
                soname != "libzstd.so.1",
                "%s contains forbidden dynamic zstd" % label,
            )
            path = _logical_path(
                provider["path"], soname, provider_label + " path"
            )
            owner = provider["owner"]
            require(
                type(owner) is str and OWNER_RE.match(owner),
                "%s owner reference is invalid" % provider_label,
            )
            require(
                owner == expected_owner and owner in owner_records,
                "%s owner differs from fixed ownership" % provider_label,
            )
            _sha256(provider["dso_sha256"], provider_label + " dso_sha256")
            observed.append((soname, owner))
            paths.append(path)
        require(
            tuple(observed) == EXPECTED_PROVIDERS,
            "%s providers differ from the fixed ownership set" % label,
        )
        require(len(paths) == len(set(paths)), "%s repeats a provider path" % label)
        observed_arches.append(arch)
    require(
        tuple(observed_arches) == TARGET_ORDER,
        "provider policy target order differs",
    )
    return document


def policy_target(document, arch, triple=None):
    validate_policy(document)
    require(arch in TARGETS, "unsupported provider policy architecture")
    if triple is None:
        triple = TARGETS[arch]["triple"]
    matches = [
        target
        for target in document["targets"]
        if target["arch"] == arch and target["triple"] == triple
    ]
    require(len(matches) == 1, "provider policy has no unique requested target")
    return matches[0]


def _lock_packages(lock, arch, label):
    _exact_fields(lock, LOCK_KEYS, label)
    require(
        lock["$schema"] == "https://crossforge.dev/schemas/rpm-lock.schema.json"
        and type(lock["schema_version"]) is int
        and lock["schema_version"] == 1
        and lock["kind"] == "rpm-lock",
        "%s is not a supported RPM lock" % label,
    )
    reference = lock["transaction"]
    _exact_fields(reference, LOCK_REFERENCE_KEYS, label + " transaction")
    require(
        reference["file"] == TARGETS[arch]["transaction_file"],
        "%s transaction path differs from target architecture" % label,
    )
    _sha256(reference["canonical_sha256"], label + " transaction SHA256")
    packages = lock["packages"]
    require(type(packages) is list and packages, "%s packages must be nonempty" % label)
    by_name = {}
    nevras = []
    for index, package in enumerate(packages):
        package_label = "%s package %d" % (label, index)
        _exact_fields(package, LOCK_PACKAGE_KEYS, package_label)
        _exact_fields(package["header"], LOCK_HEADER_KEYS, package_label + " header")
        header = package["header"]
        require(
            package["nevra"] == header["nevra"],
            "%s NEVRA differs from verified header" % package_label,
        )
        _sha256(package["received_sha256"], package_label + " received_sha256")
        name = header["name"]
        require(type(name) is str and name, "%s package name is invalid" % package_label)
        if name in set(EXPECTED_OWNERS):
            require(
                name not in by_name,
                "%s repeats runtime owner package %s" % (label, name),
            )
            require(
                header["arch"] == arch,
                "%s runtime owner package has wrong architecture" % label,
            )
            by_name[name] = package
        nevras.append(package["nevra"])
    require(
        nevras == sorted(nevras) and len(nevras) == len(set(nevras)),
        "%s packages are not sorted and unique" % label,
    )
    require(
        set(by_name) == set(EXPECTED_OWNERS),
        "%s omits a Python runtime owner package" % label,
    )
    return by_name


def validate_policy_target_against_lock(document, arch, lock):
    """Bind one target policy to its exact embedded sysroot content lock."""
    validate_policy(document)
    require(arch in TARGETS, "unsupported provider policy architecture")
    label = "%s sysroot lock" % arch
    packages = _lock_packages(lock, arch, label)
    lock_sha256 = canonical_sha256(lock)
    target = policy_target(document, arch)
    require(
        target["sysroot_lock"]["canonical_sha256"] == lock_sha256,
        "%s target sysroot lock SHA256 differs" % arch,
    )
    for owner in target["owners"]:
        package = packages[owner["name"]]
        require(
            owner
            == {
                "name": package["header"]["name"],
                "nevra": package["nevra"],
                "received_sha256": package["received_sha256"],
            },
            "%s owner %s differs from locked RPM"
            % (arch, owner["name"]),
        )
    return {
        "sysroot_lock_sha256": lock_sha256,
        "provider_count": len(target["providers"]),
        "rpm_owner_count": len(packages),
    }


def validate_policy_against_locks(document, locks_by_arch):
    """Bind every provider to one exact package in its target sysroot lock."""
    validate_policy(document)
    require(
        type(locks_by_arch) is dict and set(locks_by_arch) == set(TARGET_ORDER),
        "provider policy locks must contain both target architectures exactly",
    )
    summary = {}
    for arch in TARGET_ORDER:
        summary[arch] = validate_policy_target_against_lock(
            document, arch, locks_by_arch[arch]
        )
    return summary


def _real_root(root, label):
    root = Path(root)
    try:
        details = os.lstat(str(root))
    except OSError as error:
        raise RuntimeProviderPolicyError("%s is unavailable: %s" % (label, error))
    require(not stat.S_ISLNK(details.st_mode), "%s must not be a symlink" % label)
    require(stat.S_ISDIR(details.st_mode), "%s is not a directory" % label)
    return Path(os.path.realpath(str(root)))


def provider_file_sha256(root, provider, label):
    """Hash one logical provider path without permitting root escape."""
    _exact_fields(provider, PROVIDER_KEYS, label + " provider")
    root = _real_root(root, label + " root")
    logical = provider["path"]
    relative = PurePosixPath(logical.lstrip("/"))
    require(
        logical.startswith("/usr/lib64/")
        and not relative.is_absolute()
        and ".." not in relative.parts,
        "%s provider path is unsafe" % label,
    )
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            details = os.lstat(str(current))
        except OSError as error:
            raise RuntimeProviderPolicyError(
                "%s provider parent is unavailable: %s" % (label, error)
            )
        require(
            not stat.S_ISLNK(details.st_mode),
            "%s provider parent is a symlink" % label,
        )
        require(stat.S_ISDIR(details.st_mode), "%s provider parent is not a directory" % label)
    candidate = root.joinpath(*relative.parts)
    require(os.path.lexists(str(candidate)), "%s provider file is missing" % label)
    resolved = Path(os.path.realpath(str(candidate)))
    require(
        resolved == root or root in resolved.parents,
        "%s provider file escapes its root" % label,
    )
    require(resolved.is_file(), "%s provider path is not a regular file" % label)
    observed = file_sha256(resolved)
    require(
        observed == provider["dso_sha256"],
        "%s provider DSO SHA256 differs" % label,
    )
    return observed


def validate_provider_roots(document, sysroot_roots, clean_roots):
    """Verify reviewed bytes and clean/sysroot equality for both targets."""
    validate_policy(document)
    for roots, label in (
        (sysroot_roots, "sysroot"),
        (clean_roots, "clean runtime"),
    ):
        require(
            type(roots) is dict and set(roots) == set(TARGET_ORDER),
            "%s roots must contain both target architectures exactly" % label,
        )
    result = {}
    for arch in TARGET_ORDER:
        target = policy_target(document, arch)
        records = []
        for provider in target["providers"]:
            sysroot_sha256 = provider_file_sha256(
                sysroot_roots[arch], provider, "%s sysroot %s" % (arch, provider["soname"])
            )
            clean_sha256 = provider_file_sha256(
                clean_roots[arch], provider, "%s clean %s" % (arch, provider["soname"])
            )
            require(
                clean_sha256 == sysroot_sha256,
                "%s provider %s clean/sysroot bytes differ"
                % (arch, provider["soname"]),
            )
            records.append(
                {"soname": provider["soname"], "sha256": sysroot_sha256}
            )
        result[arch] = records
    return result


def runtime_provider_evidence(document, arch, root=None):
    """Return the canonical target policy projection, optionally hashing a root."""
    target = policy_target(document, arch)
    owners = {owner["name"]: owner for owner in target["owners"]}
    records = []
    for provider in target["providers"]:
        if root is not None:
            provider_file_sha256(
                root,
                provider,
                "%s runtime %s" % (arch, provider["soname"]),
            )
        records.append(
            {
                "soname": provider["soname"],
                "path": provider["path"],
                "owner": dict(owners[provider["owner"]]),
                "dso_sha256": provider["dso_sha256"],
            }
        )
    return {
        "policy_sha256": canonical_sha256(document),
        "target": {"arch": arch, "triple": target["triple"]},
        "sysroot_lock_sha256": target["sysroot_lock"]["canonical_sha256"],
        "provider_catalog_sha256": target["provider_catalog_sha256"],
        "providers": records,
    }


def _validate_schema_identity(schema):
    require(type(schema) is dict, "provider policy schema must be an object")
    require(schema.get("$schema") == SCHEMA_DRAFT, "provider policy schema draft differs")
    require(schema.get("$id") == SCHEMA_ID, "provider policy schema id differs")


def validate_repository(repository=REPOSITORY, provider_roots=None):
    """Validate the checked-in policy, schema and both complete RPM locks."""
    repository = Path(repository)
    require(repository.resolve() == REPOSITORY, "repository validation path differs")
    release = load_json(repository / "config/release.json")
    try:
        release_abi = ABI_CONTRACT["validate_release_abi_identities"](
            release
        )
    except ABI_CONTRACT["AbiContractError"] as error:
        raise RuntimeProviderPolicyError(str(error)) from error
    policy = load_json(repository / "config/python-runtime-providers.json")
    schema = load_json(
        repository / "config/schemas/python-runtime-providers.schema.json"
    )
    _validate_schema_identity(schema)
    locks = {
        arch: load_json(repository / TARGETS[arch]["lock_file"])
        for arch in TARGET_ORDER
    }
    summary = validate_policy_against_locks(policy, locks)
    release_python_abi = release_abi["python"]
    require(
        release_python_abi["runtime_provider_policy"]
        == {
            "file": "config/python-runtime-providers.json",
            "canonical_sha256": canonical_sha256(policy),
        },
        "release Python runtime provider policy identity differs",
    )
    python_abi = runpy.run_path(
        str(repository / "scripts/python_abi_audit.py")
    )
    for arch in TARGET_ORDER:
        target = policy_target(policy, arch)
        catalog_path = repository / (
            "evidence/abi/el8-%s-python-provider-catalog.json" % arch
        )
        catalog = load_json(catalog_path)
        require(
            catalog_path.read_bytes() == canonical_bytes(catalog) + b"\n",
            "%s provider catalog is not canonical JSON" % arch,
        )
        baseline = ABI_CONTRACT["load_baseline"](
            repository / ("abi/el8/%s.json" % arch),
            arch,
            TARGETS[arch]["triple"],
        )
        try:
            python_abi["validate_provider_catalog"](
                baseline,
                [provider["soname"] for provider in target["providers"]],
                catalog,
            )
        except python_abi["PythonAbiAuditError"] as error:
            raise RuntimeProviderPolicyError(str(error)) from error
        require(
            canonical_sha256(catalog) == target["provider_catalog_sha256"],
            "%s provider catalog digest differs from policy" % arch,
        )
        require(
            release_python_abi["provider_catalogs"][arch]
            == {
                "file": (
                    "evidence/abi/el8-%s-python-provider-catalog.json"
                    % arch
                ),
                "canonical_sha256": canonical_sha256(catalog),
            },
            "%s release Python provider catalog identity differs" % arch,
        )
        summary[arch]["provider_catalog_sha256"] = target[
            "provider_catalog_sha256"
        ]

    rpm_validator = runpy.run_path(
        str(repository / "scripts/validate-rpm-lock.py")
    )
    for arch in TARGET_ORDER:
        try:
            rpm_validator["validate_schema"](locks[arch])
            transaction = rpm_validator["validate_lock_semantics"](locks[arch])
        except rpm_validator["ValidationError"] as error:
            raise RuntimeProviderPolicyError(
                "%s sysroot lock is invalid: %s" % (arch, error)
            ) from error
        identity = transaction["identity"]
        require(
            identity["role"] == "target-sysroot"
            and identity["arch"] == arch
            and identity["target_triple"] == TARGETS[arch]["triple"],
            "%s sysroot transaction identity differs" % arch,
        )

    if provider_roots is not None:
        base = Path(provider_roots)
        validate_provider_roots(
            policy,
            {arch: base / "sysroot" / arch for arch in TARGET_ORDER},
            {arch: base / "clean" / arch for arch in TARGET_ORDER},
        )
    return {
        "policy_sha256": canonical_sha256(policy),
        "targets": summary,
        "runtime_contract": dict(RUNTIME_CONTRACT),
    }

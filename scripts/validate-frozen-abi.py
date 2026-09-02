#!/usr/bin/env python3
"""Validate the complete, fixed EL8 ABI repository contract.

This is a read-only repository gate, not a general ABI inspection command.
It always validates both supported targets and their fixed baseline, clean
inventory, sysroot inventory, and extraction-evidence paths.  Only Python
3.6-compatible standard-library interfaces are used.
"""

import json
import os
import runpy
import stat
import sys
from pathlib import Path

import abi_contract


REPOSITORY = Path(__file__).resolve().parents[1]
BASELINE = "el8"
ARCHES = ("x86_64", "aarch64")
SOURCE_VARIANTS = (
    (
        "clean",
        "clean-rocky-oci",
        "docker-copy-from-release-pinned-oci-child",
    ),
    (
        "sysroot",
        "locked-sysroot",
        "verified-materialization-of-release-pinned-sysroot-lock",
    ),
)

EVIDENCE_KIND = "crossforge-abi-extraction-evidence"
EVIDENCE_KEYS = {
    "schema_version",
    "kind",
    "target",
    "source",
    "root_trust_boundary",
    "provider_manifest",
    "tool",
    "commands",
    "providers",
    "inventory",
}
EVIDENCE_MANIFEST_KEYS = {"path", "canonical_sha256"}
EVIDENCE_TOOL_KEYS = {"name", "command", "version"}
EVIDENCE_COMMAND_KEYS = {"provider", "operation", "arguments"}
EVIDENCE_PROVIDER_KEYS = {"soname", "path", "sha256", "counts"}
EVIDENCE_COUNT_KEYS = {
    "public_versioned_exports",
    "unversioned_exports",
    "nonpublic_versioned_exports",
}
EVIDENCE_INVENTORY_KEYS = {"canonical_sha256", "provider_count"}
EMPTY_DIFF = {
    "missing_providers": [],
    "extra_providers": [],
    "missing_exports": {},
    "extra_exports": {},
}


def fail(message):
    raise abi_contract.AbiContractError(message)


def require(condition, message):
    if not condition:
        fail(message)


def _require_real_directory(path, label):
    try:
        details = os.lstat(str(path))
    except OSError as error:
        fail("%s is unavailable: %s" % (label, error))
    require(
        not stat.S_ISLNK(details.st_mode),
        "%s must not be a symbolic link" % label,
    )
    require(stat.S_ISDIR(details.st_mode), "%s is not a directory" % label)


def _fixed_regular_file(repository, components, label):
    current = repository
    _require_real_directory(current, "repository root")
    for component in components[:-1]:
        current = current / component
        _require_real_directory(current, label + " parent")
    path = current / components[-1]
    try:
        details = os.lstat(str(path))
    except OSError as error:
        fail("%s is unavailable: %s" % (label, error))
    require(
        not stat.S_ISLNK(details.st_mode),
        "%s must not be a symbolic link" % label,
    )
    require(stat.S_ISREG(details.st_mode), "%s is not a regular file" % label)
    return path


def _load_fixed_json(path, label, require_canonical=False):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(str(path), flags)
        details = os.fstat(descriptor)
        require(stat.S_ISREG(details.st_mode), "%s is not a regular file" % label)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            payload = stream.read()
            document = json.loads(
                payload,
                object_pairs_hook=abi_contract.reject_duplicate_keys,
                parse_constant=abi_contract.reject_nonfinite_constant,
                parse_float=abi_contract.parse_finite_float,
            )
        if require_canonical:
            require(
                payload.encode("utf-8")
                == abi_contract.canonical_bytes(document) + b"\n",
                "%s is not canonical JSON" % label,
            )
        return document
    except abi_contract.AbiContractError:
        raise
    except (OSError, ValueError) as error:
        fail("%s: %s" % (path, error))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_release(release, schema, validator):
    try:
        validator["validate_schema_subset"](schema)
        validator["validate"](release, schema, schema, "$")
    except validator["ValidationError"] as error:
        fail("release validation failed: %s" % error)


def _release_identities(release):
    require(type(release) is dict, "release configuration must be an object")
    require(
        release.get("baseline") == BASELINE,
        "release baseline differs from frozen ABI baseline",
    )
    try:
        manifests = release["base_image"]["manifests"]
        targets = release["targets"]
    except (KeyError, TypeError):
        fail("release configuration omits ABI source identities")
    require(
        type(manifests) is dict,
        "release base-image manifests must be an object",
    )
    require(type(targets) is list, "release targets must be an array")

    identities = {}
    for arch, platform in (("x86_64", "amd64"), ("aarch64", "arm64")):
        digest = manifests.get(platform) if type(manifests) is dict else None
        require(
            type(digest) is str and digest.startswith("sha256:"),
            "release base-image child digest is invalid for %s" % arch,
        )
        identity = digest[len("sha256:") :]
        abi_contract._validate_sha256(identity, "release base-image child digest")
        identities[(arch, "clean")] = identity

        matches = [
            target
            for target in targets
            if type(target) is dict
            and target.get("arch") == arch
            and target.get("triple") == abi_contract.TARGETS[arch]["triple"]
        ]
        require(len(matches) == 1, "release has no unique target for %s" % arch)
        sysroot = matches[0].get("sysroot")
        require(
            type(sysroot) is dict and sysroot.get("status") == "locked",
            "release sysroot is not locked for %s" % arch,
        )
        identity = sysroot.get("canonical_sha256")
        abi_contract._validate_sha256(identity, "release sysroot lock digest")
        identities[(arch, "sysroot")] = identity
    return identities


def _provider_names(manifest, arch, triple):
    target = abi_contract.provider_manifest_target(manifest, arch, triple)
    return [provider["soname"] for provider in target["providers"]]


def _validate_provider_order(document, expected, label):
    require(
        list(document["providers"]) == expected,
        "%s provider order differs from the fixed provider manifest" % label,
    )


def _expected_provider_evidence(inventory, provider_names):
    result = []
    for soname in provider_names:
        provider = inventory["providers"][soname]
        result.append(
            {
                "soname": soname,
                "path": provider["path"],
                "sha256": provider["sha256"],
                "counts": {
                    "public_versioned_exports": len(provider["exports"]),
                    "unversioned_exports": len(provider["unversioned_exports"]),
                    "nonpublic_versioned_exports": len(
                        provider["nonpublic_versioned_exports"]
                    ),
                },
            }
        )
    return result


def _expected_commands(provider_names, inventory):
    operations = (
        ("elf_header", "-h"),
        ("dynamic_section", "-d"),
        ("dynamic_symbols", "--dyn-syms"),
    )
    result = []
    for soname in provider_names:
        logical_path = inventory["providers"][soname]["path"]
        for operation, flag in operations:
            result.append(
                {
                    "provider": soname,
                    "operation": operation,
                    "arguments": ["readelf", "--wide", flag, logical_path],
                }
            )
    return result


def _validate_extraction_evidence(
    evidence,
    inventory,
    manifest_sha256,
    provider_names,
    trust_boundary,
    label,
):
    require(
        type(evidence) is dict and set(evidence) == EVIDENCE_KEYS,
        "%s fields differ" % label,
    )
    require(
        type(evidence["schema_version"]) is int
        and evidence["schema_version"] == 1,
        "%s schema version is unsupported" % label,
    )
    require(evidence["kind"] == EVIDENCE_KIND, "%s kind is unsupported" % label)
    require(
        evidence["target"] == inventory["target"],
        "%s target differs from inventory" % label,
    )
    require(
        evidence["source"] == inventory["source"],
        "%s source differs from inventory" % label,
    )
    require(
        evidence["root_trust_boundary"] == trust_boundary,
        "%s root trust boundary differs" % label,
    )

    provider_manifest = evidence["provider_manifest"]
    require(
        type(provider_manifest) is dict
        and set(provider_manifest) == EVIDENCE_MANIFEST_KEYS,
        "%s provider_manifest fields differ" % label,
    )
    require(
        provider_manifest
        == {
            "path": "config/abi-providers.json",
            "canonical_sha256": manifest_sha256,
        },
        "%s provider manifest binding differs" % label,
    )

    tool = evidence["tool"]
    require(
        type(tool) is dict and set(tool) == EVIDENCE_TOOL_KEYS,
        "%s tool fields differ" % label,
    )
    require(tool.get("name") == "readelf", "%s tool name differs" % label)
    require(tool.get("command") == "readelf", "%s readelf command is not fixed" % label)
    version = tool.get("version")
    require(
        type(version) is str
        and version.startswith("GNU readelf (")
        and version.strip() == version
        and "\n" not in version,
        "%s readelf version is invalid" % label,
    )

    providers = evidence["providers"]
    require(type(providers) is list, "%s providers must be an array" % label)
    for index, provider in enumerate(providers):
        item_label = "%s providers[%d]" % (label, index)
        require(
            type(provider) is dict and set(provider) == EVIDENCE_PROVIDER_KEYS,
            "%s fields differ" % item_label,
        )
        counts = provider["counts"]
        require(
            type(counts) is dict and set(counts) == EVIDENCE_COUNT_KEYS,
            "%s counts fields differ" % item_label,
        )
        require(
            all(type(value) is int and value >= 0 for value in counts.values()),
            "%s counts must be non-negative integers" % item_label,
        )
    require(
        providers == _expected_provider_evidence(inventory, provider_names),
        "%s provider path, SHA256, count, membership, or order differs" % label,
    )

    commands = evidence["commands"]
    require(type(commands) is list, "%s commands must be an array" % label)
    for index, command in enumerate(commands):
        require(
            type(command) is dict and set(command) == EVIDENCE_COMMAND_KEYS,
            "%s commands[%d] fields differ" % (label, index),
        )
    require(
        commands == _expected_commands(provider_names, inventory),
        "%s readelf operations, logical paths, membership, or order differ" % label,
    )

    inventory_record = evidence["inventory"]
    require(
        type(inventory_record) is dict
        and set(inventory_record) == EVIDENCE_INVENTORY_KEYS,
        "%s inventory fields differ" % label,
    )
    require(
        type(inventory_record["provider_count"]) is int
        and inventory_record["provider_count"] == len(provider_names),
        "%s provider_count differs" % label,
    )
    require(
        inventory_record["canonical_sha256"]
        == abi_contract.canonical_sha256(inventory),
        "%s canonical inventory digest differs" % label,
    )
    return dict(tool)


def validate_documents(release, manifest, baselines, inventories, extractions):
    """Validate an already-loaded fixed matrix and return a compact summary."""
    abi_contract.validate_provider_manifest(manifest)
    manifest_sha256 = abi_contract.canonical_sha256(manifest)
    identities = _release_identities(release)
    release_abi = abi_contract.validate_release_abi_identities(release)
    require(
        release_abi["provider_manifest"]["canonical_sha256"]
        == manifest_sha256,
        "release ABI provider manifest digest differs from repository",
    )
    require(
        type(baselines) is dict and set(baselines) == set(ARCHES),
        "frozen ABI baseline matrix differs",
    )
    require(
        type(inventories) is dict and set(inventories) == set(ARCHES),
        "frozen ABI inventory matrix differs",
    )
    require(
        type(extractions) is dict and set(extractions) == set(ARCHES),
        "ABI extraction evidence matrix differs",
    )

    summaries = []
    readelf_tool = None
    for arch in ARCHES:
        triple = abi_contract.TARGETS[arch]["triple"]
        require(
            type(inventories[arch]) is dict
            and set(inventories[arch]) == {"clean", "sysroot"},
            "ABI inventory variants differ for %s" % arch,
        )
        require(
            type(extractions[arch]) is dict
            and set(extractions[arch]) == {"clean", "sysroot"},
            "ABI extraction variants differ for %s" % arch,
        )
        provider_names = _provider_names(manifest, arch, triple)

        validated_inventories = {}
        for variant, source_kind, _trust_boundary in SOURCE_VARIANTS:
            inventory = inventories[arch][variant]
            label = "%s %s inventory" % (arch, variant)
            abi_contract.validate_inventory(inventory, arch, triple)
            abi_contract.validate_inventory_provider_manifest(inventory, manifest)
            require(
                inventory["source"]
                == {
                    "kind": source_kind,
                    "identity_sha256": identities[(arch, variant)],
                    "provider_manifest_sha256": manifest_sha256,
                },
                "%s source identity or manifest binding differs from release"
                % label,
            )
            _validate_provider_order(inventory, provider_names, label)
            validated_inventories[variant] = inventory

        baseline = baselines[arch]
        abi_contract.validate_baseline(baseline, arch, triple)
        release_target_abi = release_abi["targets"][arch]
        require(
            release_target_abi["baseline"]["canonical_sha256"]
            == abi_contract.canonical_sha256(baseline)
            and release_target_abi["sysroot_inventory"][
                "canonical_sha256"
            ]
            == abi_contract.canonical_sha256(
                validated_inventories["sysroot"]
            ),
            "%s release ABI identities differ from repository" % arch,
        )
        require(baseline["baseline"] == BASELINE, "ABI baseline id differs for %s" % arch)
        _validate_provider_order(baseline, provider_names, "%s baseline" % arch)
        clean_difference = abi_contract.validate_baseline_against_inventory(
            baseline, validated_inventories["clean"], require_exact=True
        )
        require(
            clean_difference == EMPTY_DIFF,
            "%s baseline does not exactly equal clean public exports" % arch,
        )

        sysroot_difference = abi_contract.validate_inventory_superset(
            validated_inventories["sysroot"], baseline
        )
        require(
            sysroot_difference == EMPTY_DIFF,
            "%s sysroot inventory has unreviewed ABI extras" % arch,
        )

        for variant, _source_kind, trust_boundary in SOURCE_VARIANTS:
            observed_tool = _validate_extraction_evidence(
                extractions[arch][variant],
                validated_inventories[variant],
                manifest_sha256,
                provider_names,
                trust_boundary,
                "%s %s extraction evidence" % (arch, variant),
            )
            if readelf_tool is None:
                readelf_tool = observed_tool
            else:
                require(
                    observed_tool == readelf_tool,
                    "ABI extraction evidence readelf tool provenance differs",
                )

        summaries.append(
            {
                "arch": arch,
                "provider_count": len(provider_names),
                "public_export_count": sum(
                    len(exports) for exports in baseline["providers"].values()
                ),
                "baseline_sha256": abi_contract.canonical_sha256(baseline),
            }
        )
    return {
        "baseline": BASELINE,
        "provider_manifest_sha256": manifest_sha256,
        "targets": summaries,
    }


def load_repository_documents(repository=None):
    """Strictly load only the ten fixed ABI documents and release inputs."""
    if repository is None:
        repository = REPOSITORY
    repository = Path(repository)
    if not repository.is_absolute():
        repository = Path.cwd() / repository
    _require_real_directory(repository, "repository root")

    release_path = _fixed_regular_file(
        repository, ("config", "release.json"), "release configuration"
    )
    schema_path = _fixed_regular_file(
        repository,
        ("config", "schemas", "release.schema.json"),
        "release schema",
    )
    validator_path = _fixed_regular_file(
        repository, ("scripts", "validate-release.py"), "release validator"
    )
    manifest_path = _fixed_regular_file(
        repository,
        ("config", "abi-providers.json"),
        "fixed ABI provider manifest",
    )
    release = _load_fixed_json(release_path, "release configuration")
    schema = _load_fixed_json(schema_path, "release schema")
    validator = runpy.run_path(str(validator_path))
    _validate_release(release, schema, validator)
    manifest = _load_fixed_json(manifest_path, "fixed ABI provider manifest")

    baselines = {}
    inventories = {}
    extractions = {}
    for arch in ARCHES:
        baselines[arch] = _load_fixed_json(
            _fixed_regular_file(
                repository,
                ("abi", BASELINE, arch + ".json"),
                "%s frozen ABI baseline" % arch,
            ),
            "%s frozen ABI baseline" % arch,
            require_canonical=True,
        )
        inventories[arch] = {}
        extractions[arch] = {}
        for variant, _source_kind, _trust_boundary in SOURCE_VARIANTS:
            stem = "%s-%s-%s" % (BASELINE, arch, variant)
            inventories[arch][variant] = _load_fixed_json(
                _fixed_regular_file(
                    repository,
                    ("evidence", "abi", stem + ".json"),
                    "%s ABI inventory" % stem,
                ),
                "%s ABI inventory" % stem,
                require_canonical=True,
            )
            extractions[arch][variant] = _load_fixed_json(
                _fixed_regular_file(
                    repository,
                    ("evidence", "abi", stem + ".extraction.json"),
                    "%s ABI extraction evidence" % stem,
                ),
                "%s ABI extraction evidence" % stem,
                require_canonical=True,
            )
    return release, manifest, baselines, inventories, extractions


def validate_repository(repository=None):
    return validate_documents(*load_repository_documents(repository))


def format_summary(summary):
    targets = ", ".join(
        "%s=%d providers/%d exports"
        % (
            target["arch"],
            target["provider_count"],
            target["public_export_count"],
        )
        for target in summary["targets"]
    )
    return "frozen ABI %s valid: %s; sysroot extras=0" % (
        summary["baseline"],
        targets,
    )


def main():
    if len(sys.argv) != 1:
        print("usage: validate-frozen-abi.py", file=sys.stderr)
        return 2
    try:
        summary = validate_repository()
    except abi_contract.AbiContractError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

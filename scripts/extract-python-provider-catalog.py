#!/usr/bin/env python3
"""Materialize one reviewed Python provider ELF catalog from a locked root."""

import argparse
import os
import sys
import tempfile
from pathlib import Path

import abi_contract
import python_abi_audit
import python_runtime_providers


def fail(message):
    raise python_runtime_providers.RuntimeProviderPolicyError(message)


def contained_path(root, logical, label):
    root = Path(root).resolve()
    candidate = root / logical.lstrip("/")
    resolved = candidate.resolve()
    if root not in resolved.parents or not resolved.is_file():
        fail("%s escapes or is missing from the provider root" % label)
    return candidate


def build_catalog(repository, root, arch, readelf):
    target = abi_contract.TARGETS[arch]
    baseline = abi_contract.load_baseline(
        repository / ("abi/el8/%s.json" % arch),
        arch,
        target["triple"],
    )
    inventory = abi_contract.load_inventory(
        repository / ("evidence/abi/el8-%s-sysroot.json" % arch),
        arch,
        target["triple"],
    )
    policy = python_runtime_providers.load_json(
        repository / "config/python-runtime-providers.json"
    )
    runtime_target = python_runtime_providers.policy_target(
        policy, arch, target["triple"]
    )
    catalog = {}
    for soname, provider in inventory["providers"].items():
        path = contained_path(root, provider["path"], soname)
        if python_runtime_providers.file_sha256(path.resolve()) != provider["sha256"]:
            fail("core provider SHA256 differs: %s" % soname)
        _evidence, record = python_abi_audit.elf_record_from_file(
            readelf, path, soname, expected_soname=soname
        )
        catalog[soname] = record
    for provider in runtime_target["providers"]:
        python_runtime_providers.provider_file_sha256(
            root, provider, "%s %s" % (arch, provider["soname"])
        )
        path = contained_path(root, provider["path"], provider["soname"])
        _evidence, record = python_abi_audit.elf_record_from_file(
            readelf,
            path,
            provider["soname"],
            expected_soname=provider["soname"],
        )
        catalog[provider["soname"]] = record
    external = [provider["soname"] for provider in runtime_target["providers"]]
    python_abi_audit.validate_provider_catalog(baseline, external, catalog)
    digest = python_runtime_providers.canonical_sha256(catalog)
    if digest != runtime_target["provider_catalog_sha256"]:
        fail("provider catalog differs from the reviewed policy pin")
    return catalog


def publish(path, document):
    if path.exists() or path.is_symlink():
        fail("refusing to replace provider catalog: %s" % path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.tmp-" % path.name, dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(python_runtime_providers.canonical_bytes(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--arch", choices=("aarch64", "x86_64"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--readelf", default="readelf")
    arguments = parser.parse_args()
    output = repository / (
        "evidence/abi/el8-%s-python-provider-catalog.json" % arguments.arch
    )
    catalog = build_catalog(
        repository, arguments.root, arguments.arch, arguments.readelf
    )
    publish(output, catalog)
    print(
        "%s %s"
        % (
            output.relative_to(repository),
            python_runtime_providers.canonical_sha256(catalog),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        abi_contract.AbiContractError,
        python_abi_audit.PythonAbiAuditError,
        python_runtime_providers.RuntimeProviderPolicyError,
        OSError,
    ) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)

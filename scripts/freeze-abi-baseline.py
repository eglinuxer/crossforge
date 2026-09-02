#!/usr/bin/env python3
"""Promote one explicitly reviewed clean ABI inventory into a frozen floor.

This is deliberately not a general JSON converter.  The architecture selects
one fixed repository input and one fixed repository output.  Promotion also
requires the caller to repeat the candidate's canonical SHA256, so generating
new evidence cannot silently update the reviewed ABI contract.

Only Python 3.6-compatible standard-library interfaces are used.
"""

import argparse
import copy
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import abi_contract


REPOSITORY = Path(__file__).resolve().parents[1]
BASELINE = "el8"


def fail(message):
    raise abi_contract.AbiContractError(message)


def _require_real_directory(path, label):
    try:
        details = os.lstat(str(path))
    except OSError as error:
        fail("%s is unavailable: %s" % (label, error))
    if stat.S_ISLNK(details.st_mode):
        fail("%s must not be a symbolic link" % label)
    if not stat.S_ISDIR(details.st_mode):
        fail("%s is not a directory" % label)


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
    if stat.S_ISLNK(details.st_mode):
        fail("%s must not be a symbolic link" % label)
    if not stat.S_ISREG(details.st_mode):
        fail("%s is not a regular file" % label)
    return path


def _load_fixed_json(path, label):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(str(path), flags)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            fail("%s is not a regular file" % label)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            return json.load(
                stream,
                object_pairs_hook=abi_contract.reject_duplicate_keys,
                parse_constant=abi_contract.reject_nonfinite_constant,
                parse_float=abi_contract.parse_finite_float,
            )
    except abi_contract.AbiContractError:
        raise
    except (OSError, ValueError) as error:
        fail("%s: %s" % (path, error))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _ensure_output_parent(repository):
    current = repository
    _require_real_directory(current, "repository root")
    for component in ("abi", BASELINE):
        current = current / component
        try:
            os.mkdir(str(current), 0o755)
        except FileExistsError:
            pass
        except OSError as error:
            fail("cannot create fixed ABI output directory: %s" % error)
        _require_real_directory(current, "fixed ABI output directory")
    return current


def _profile(interpreter, bind_now):
    return {
        "textrel": "forbid",
        "relr": "forbid",
        "rpath": "forbid",
        "runpath": "forbid",
        "gnu_stack": "require-non-executable",
        "writable_executable_segments": "forbid",
        "interpreter": {"decision": "require", "expected": interpreter},
        "relro": "require",
        "bind_now": bind_now,
    }


def baseline_from_inventory(inventory, inventory_sha256):
    arch = inventory["target"]["arch"]
    interpreter = abi_contract.TARGETS[arch]["interpreter"]
    return {
        "$schema": abi_contract.BASELINE_SCHEMA_ID,
        "schema_version": 1,
        "kind": abi_contract.BASELINE_KIND,
        "baseline": BASELINE,
        "target": copy.deepcopy(inventory["target"]),
        "review": {
            "status": "reviewed",
            "source_inventory": "evidence/abi/%s-%s-clean.json"
            % (BASELINE, arch),
            "source_inventory_sha256": inventory_sha256,
        },
        "providers": {
            soname: copy.deepcopy(provider["exports"])
            for soname, provider in inventory["providers"].items()
        },
        "elf_policy": {
            "profiles": {
                "compiler-default-observation": _profile(
                    interpreter, "require-absent"
                ),
                "crossforge-qualified-v1": _profile(interpreter, "require"),
            },
            "artifact_exceptions": copy.deepcopy(
                abi_contract.EXPECTED_ARTIFACT_EXCEPTIONS
            ),
        },
    }


def _publish_new(path, document):
    if os.path.lexists(str(path)):
        fail("refusing to replace existing ABI baseline: %s" % path)
    payload = abi_contract.canonical_bytes(document) + b"\n"
    descriptor = None
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s.tmp-" % path.name,
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path), follow_symlinks=False)
        except FileExistsError:
            fail("refusing to replace existing ABI baseline: %s" % path)
        except OSError as error:
            fail("cannot publish ABI baseline atomically: %s" % error)
        os.unlink(str(temporary))
        temporary = None
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory = os.open(str(path.parent), directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and os.path.lexists(str(temporary)):
            os.unlink(str(temporary))


def freeze(arch, accept_inventory_sha256, repository=None):
    if repository is None:
        repository = REPOSITORY
    repository = Path(repository)
    if not repository.is_absolute():
        repository = Path.cwd() / repository
    _require_real_directory(repository, "repository root")
    abi_contract._validate_sha256(
        accept_inventory_sha256, "accepted ABI inventory SHA256"
    )
    triple = abi_contract.TARGETS[arch]["triple"]

    manifest_path = _fixed_regular_file(
        repository,
        ("config", "abi-providers.json"),
        "fixed ABI provider manifest",
    )
    inventory_path = _fixed_regular_file(
        repository,
        ("evidence", "abi", "%s-%s-clean.json" % (BASELINE, arch)),
        "reviewed clean ABI inventory",
    )
    manifest = _load_fixed_json(manifest_path, "fixed ABI provider manifest")
    abi_contract.validate_provider_manifest(manifest)
    inventory = _load_fixed_json(inventory_path, "reviewed clean ABI inventory")
    abi_contract.validate_inventory(inventory, arch, triple)
    abi_contract.validate_inventory_provider_manifest(inventory, manifest)

    inventory_sha256 = abi_contract.canonical_sha256(inventory)
    abi_contract.require(
        inventory_sha256 == accept_inventory_sha256,
        "accepted ABI inventory SHA256 differs from the reviewed clean inventory",
    )
    baseline = baseline_from_inventory(inventory, inventory_sha256)

    # These are the final promotion boundary: the generated document must pass
    # the independent strict validator and match every public export exactly.
    abi_contract.validate_baseline(baseline, arch, triple)
    difference = abi_contract.validate_baseline_against_inventory(
        baseline, inventory, require_exact=True
    )
    abi_contract.require(
        difference
        == {
            "missing_providers": [],
            "extra_providers": [],
            "missing_exports": {},
            "extra_exports": {},
        },
        "generated ABI baseline does not exactly match the clean inventory",
    )

    output_parent = _ensure_output_parent(repository)
    output_path = output_parent / (arch + ".json")
    _publish_new(output_path, baseline)
    return baseline, output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=sorted(abi_contract.TARGETS))
    parser.add_argument("--accept-inventory-sha256", required=True)
    arguments = parser.parse_args()
    try:
        baseline, output_path = freeze(
            arguments.arch, arguments.accept_inventory_sha256
        )
    except abi_contract.AbiContractError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(
        "%s %s"
        % (output_path.relative_to(REPOSITORY), abi_contract.canonical_sha256(baseline))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

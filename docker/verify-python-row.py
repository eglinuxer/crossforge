#!/usr/bin/env python3
"""Verify CPython row metadata against release and component identities."""

import argparse
import hashlib
import json
import re
import runpy
import sys
from pathlib import Path


class RowError(RuntimeError):
    pass


def support_script(name):
    sibling = Path(__file__).with_name(name)
    if sibling.is_file():
        return sibling
    repository_script = Path(__file__).resolve().parents[1] / "scripts" / name
    if repository_script.is_file():
        return repository_script
    raise RowError("missing row support script: %s" % name)


ROW_CONTRACT = None
PREPARER = None
SOURCE_BINDING = None


def row_contract_tools():
    global ROW_CONTRACT
    if ROW_CONTRACT is None:
        ROW_CONTRACT = runpy.run_path(
            str(support_script("python_row_contract.py"))
        )
    return ROW_CONTRACT


def preparation_tools():
    global PREPARER
    if PREPARER is None:
        PREPARER = runpy.run_path(
            str(support_script("prepare-cpython-source.py"))
        )
    return PREPARER


def source_binding_tools():
    global SOURCE_BINDING
    if SOURCE_BINDING is None:
        SOURCE_BINDING = runpy.run_path(
            str(support_script("python_source_release_binding.py"))
        )
    return SOURCE_BINDING


def require(condition, message):
    if not condition:
        raise RowError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RowError("%s: %s" % (path, error)) from error


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def row_contract(release, row, version, adapter):
    tools = row_contract_tools()
    try:
        binding = tools["bind_release"](
            release, version=version, adapter=adapter
        )
    except tools["ContractError"] as error:
        raise RowError(str(error)) from error
    contract = binding["contract"]
    selected = binding["entry"]
    require(contract["row"] == row, "row does not match CPython implementation")
    source = selected.get("source")
    require(isinstance(source, dict), "release CPython source is missing")
    require(source.get("status") == "locked", "CPython source is not locked")
    require(
        isinstance(source.get("size"), int)
        and not isinstance(source.get("size"), bool)
        and source["size"] > 0,
        "CPython source size is invalid",
    )
    require(
        isinstance(source.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]),
        "CPython source digest is invalid",
    )
    patches = selected.get("patches", [])
    require(isinstance(patches, list), "CPython patch list is invalid")
    normalized_patches = []
    for index, patch in enumerate(patches):
        require(isinstance(patch, dict), "CPython patch %d is invalid" % index)
        require(
            set(patch) == {"file", "sha256"},
            "CPython patch %d has unexpected fields" % index,
        )
        require(
            isinstance(patch["file"], str) and patch["file"],
            "CPython patch %d path is invalid" % index,
        )
        require(
            isinstance(patch["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", patch["sha256"]),
            "CPython patch %d digest is invalid" % index,
        )
        normalized_patches.append(
            {"file": patch["file"], "sha256": patch["sha256"]}
        )
    minor = contract["minor"]
    return {
        "schema_version": 1,
        "kind": "crossforge-cpython-source-row",
        "row": row,
        "version": version,
        "minor": minor,
        "compact": minor.replace(".", ""),
        "adapter": adapter,
        "support": selected["support"],
        "release_sha256": canonical_sha256(release),
        "source": {
            "url": source["url"],
            "size": source["size"],
            "sha256": source["sha256"],
        },
        "patches": normalized_patches,
    }


def component_row_contract(
    row,
    version,
    adapter,
    source_component,
    source_component_sha256,
    policy_component,
    policy_component_sha256,
):
    tools = preparation_tools()
    try:
        entry, identities = tools["row_from_components"](
            row,
            version,
            adapter,
            source_component,
            source_component_sha256,
            policy_component,
            policy_component_sha256,
        )
    except tools["PreparationError"] as error:
        raise RowError(str(error)) from error
    require(entry["version"] == version, "component CPython version differs")
    require(entry["adapter"] == adapter, "component CPython adapter differs")
    context = {"mode": "component"}
    context.update(identities)
    return tools["_source_manifest"](
        entry, row, entry["patches"], context
    )


def verify_source_manifest(path, expected):
    actual = load_json(path)
    require(
        canonical_sha256(actual) == canonical_sha256(expected),
        "source manifest differs from authenticated row contract",
    )
    return actual


def bridge_source_manifest(manifest, release, row, version, adapter):
    """Bind one loaded v1/v2 source manifest to a complete release row."""
    tools = source_binding_tools()
    try:
        return tools["bind_source_manifest"](
            manifest, release, row, version, adapter
        )
    except tools["BindingError"] as error:
        raise RowError(str(error)) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--row", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-component", type=Path)
    parser.add_argument("--source-component-sha256")
    parser.add_argument("--policy-component", type=Path)
    parser.add_argument("--policy-component-sha256")
    arguments = parser.parse_args()

    component_values = (
        arguments.source_component,
        arguments.source_component_sha256,
        arguments.policy_component,
        arguments.policy_component_sha256,
    )
    component_mode = any(value is not None for value in component_values)
    require(
        not component_mode or all(value is not None for value in component_values),
        "source and policy component files/digests must be provided together",
    )
    require(
        (arguments.release is not None) != component_mode,
        "select exactly one full release or component input mode",
    )
    release = None
    if component_mode:
        contract = component_row_contract(
            arguments.row,
            arguments.version,
            arguments.adapter,
            arguments.source_component,
            arguments.source_component_sha256,
            arguments.policy_component,
            arguments.policy_component_sha256,
        )
    else:
        release = load_json(arguments.release)
        contract = row_contract(
            release,
            arguments.row,
            arguments.version,
            arguments.adapter,
        )
    if arguments.manifest is not None:
        if component_mode:
            verify_source_manifest(arguments.manifest, contract)
        else:
            bridge_source_manifest(
                load_json(arguments.manifest),
                release,
                arguments.row,
                arguments.version,
                arguments.adapter,
            )
    print("valid CPython row: %s %s %s" % (arguments.row, arguments.version, arguments.adapter))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, RowError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)

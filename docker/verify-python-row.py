#!/usr/bin/env python3
"""Verify generated CPython row metadata against the canonical release file."""

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


ROW_CONTRACT = runpy.run_path(str(support_script("python_row_contract.py")))
ContractError = ROW_CONTRACT["ContractError"]


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
    try:
        binding = ROW_CONTRACT["bind_release"](
            release, version=version, adapter=adapter
        )
    except ContractError as error:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--row", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--manifest", type=Path)
    arguments = parser.parse_args()

    contract = row_contract(
        load_json(arguments.release),
        arguments.row,
        arguments.version,
        arguments.adapter,
    )
    if arguments.manifest is not None:
        require(load_json(arguments.manifest) == contract, "row manifest mismatch")
    print("valid CPython row: %s %s %s" % (arguments.row, arguments.version, arguments.adapter))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, RowError, TypeError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        raise SystemExit(1)

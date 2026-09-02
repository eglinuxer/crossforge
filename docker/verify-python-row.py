#!/usr/bin/env python3
"""Verify generated CPython row metadata against the canonical release file."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


class RowError(RuntimeError):
    pass


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


def canonical_row(version):
    require(
        isinstance(version, str) and re.fullmatch(r"3\.[0-9]+\.[0-9]+", version),
        "CPython version is not an exact patch release",
    )
    minor = version.rsplit(".", 1)[0]
    return "cp" + minor.replace(".", "")


def canonical_sha256(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def row_contract(release, row, version, adapter):
    require(re.fullmatch(r"cp[0-9]+", row) is not None, "invalid CPython row")
    matches = [
        item
        for item in release.get("python", {}).get("versions", [])
        if item.get("version") == version
    ]
    require(len(matches) == 1, "release must select exactly one CPython version")
    selected = matches[0]
    require(canonical_row(version) == row, "row does not match CPython version")
    require(selected.get("adapter") == adapter, "adapter differs from release")
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
    minor = version.rsplit(".", 1)[0]
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

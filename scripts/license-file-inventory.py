#!/usr/bin/env python3
"""Create or validate the license-file inventory embedded in the SDK."""

import argparse
import hashlib
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path, PurePosixPath


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
VALIDATOR = runpy.run_path(str(SCRIPT_DIRECTORY / "validate-release.py"))
ValidationError = VALIDATOR["ValidationError"]
SCHEMA_ID = "https://crossforge.dev/schemas/license-file-inventory.schema.json"
LOGICAL_ROOTS = (PurePosixPath("/opt/crossforge"), PurePosixPath("/usr/share/licenses"))
DEFAULT_OUTPUT = PurePosixPath("/opt/crossforge/LICENSES.json")


class InventoryError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise InventoryError(message)


def canonical_sha256(document):
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_schema_document(path, schema_path):
    try:
        document = VALIDATOR["load_json"](path)
        schema = VALIDATOR["load_json"](schema_path)
        require(isinstance(document, dict), "%s must contain an object" % path)
        require(isinstance(schema, dict), "%s must contain an object" % schema_path)
        VALIDATOR["validate_schema_subset"](schema)
        VALIDATOR["validate"](document, schema, schema, "$")
        return document
    except (ValidationError, InventoryError) as error:
        raise InventoryError(str(error)) from error


def physical_path(image_root, logical_path):
    return image_root.joinpath(*logical_path.parts[1:])


def is_license_candidate(logical_path, logical_root, output_path):
    if logical_path == output_path:
        return False
    if logical_root == PurePosixPath("/usr/share/licenses"):
        return True
    lowered_parts = tuple(part.lower() for part in logical_path.parts)
    if "licenses" in lowered_parts:
        return True
    name = logical_path.name.lower()
    if name.endswith(
        (".c", ".cc", ".cmake", ".h", ".html", ".js", ".json", ".py", ".pyc")
    ):
        return False
    return (
        name.startswith("license")
        or name.startswith("licence")
        or name.startswith("copying")
        or name.startswith("notice")
        or name in ("authors", "copyright", "usage")
    )


def entry_role(logical_path):
    name = logical_path.name.lower()
    if name == "authors":
        return "authors"
    if name == "copyright":
        return "copyright"
    if name.startswith("notice"):
        return "notice"
    if name == "usage":
        return "usage"
    return "license"


def component_name(logical_path):
    parts = logical_path.parts
    if parts[:4] == ("/", "usr", "share", "licenses"):
        return "rpm/host/%s" % parts[4]
    relative = parts[3:]
    require(relative, "license path lacks a Crossforge component")
    if relative[0] == "host-tools" and len(relative) >= 2:
        return "host-tools/%s" % relative[1]
    if relative[0] == "python" and len(relative) >= 2:
        return "python/%s" % relative[1]
    if relative[0] == "sysroots" and len(relative) >= 7:
        arch = relative[2]
        try:
            marker = relative.index("licenses")
            package = relative[marker + 1]
        except (ValueError, IndexError):
            package = relative[-2]
        return "rpm/sysroot-%s/%s" % (arch, package)
    if relative[0] == "vcpkg":
        if len(relative) >= 4 and relative[1:3] == ("root", "ports"):
            return "vcpkg/port/%s" % relative[3]
        return "sources/vcpkg"
    if relative[0] == "targets" and len(relative) >= 2:
        return "toolchain/%s" % relative[1]
    if relative[0] == "share" and len(relative) >= 3:
        return "product/%s" % relative[2]
    return "sdk/%s" % relative[0]


def file_identity(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    require(size > 0, "license file is empty: %s" % path)
    return digest.hexdigest(), size


def scan_entries(image_root, output_path=DEFAULT_OUTPUT):
    image_root = image_root.resolve()
    entries = []
    seen = set()
    for logical_root in LOGICAL_ROOTS:
        root = physical_path(image_root, logical_root)
        require(root.is_dir() and not root.is_symlink(), "license root is invalid: %s" % root)
        for directory, directories, filenames in os.walk(str(root), followlinks=False):
            directories[:] = sorted(directories)
            for filename in sorted(filenames):
                path = Path(directory) / filename
                relative = path.relative_to(image_root)
                logical_path = PurePosixPath("/") / PurePosixPath(relative.as_posix())
                if not is_license_candidate(logical_path, logical_root, output_path):
                    continue
                require(not path.is_symlink(), "license path is a symlink: %s" % logical_path)
                resolved = path.resolve()
                require(
                    image_root in resolved.parents and resolved.is_file(),
                    "license path escaped the image root: %s" % logical_path,
                )
                rendered = str(logical_path)
                require(rendered not in seen, "duplicate license path: %s" % rendered)
                seen.add(rendered)
                digest, size = file_identity(resolved)
                entries.append(
                    {
                        "path": rendered,
                        "component": component_name(logical_path),
                        "role": entry_role(logical_path),
                        "sha256": digest,
                        "size": size,
                    }
                )
    entries.sort(key=lambda entry: entry["path"])
    require(entries, "license inventory is empty")
    return entries


def build_inventory(image_root, release, output_path=DEFAULT_OUTPUT):
    entries = scan_entries(image_root, output_path)
    return {
        "$schema": SCHEMA_ID,
        "schema_version": 1,
        "kind": "crossforge-license-file-inventory",
        "status": "file-inventory-not-legal-conclusion",
        "release_sha256": canonical_sha256(release),
        "roots": [str(root) for root in LOGICAL_ROOTS],
        "entry_count": len(entries),
        "entries": entries,
    }


def validate_inventory(document, schema_path, expected):
    schema = VALIDATOR["load_json"](schema_path)
    require(isinstance(schema, dict), "%s must contain an object" % schema_path)
    try:
        VALIDATOR["validate_schema_subset"](schema)
        VALIDATOR["validate"](document, schema, schema, "$")
    except ValidationError as error:
        raise InventoryError(str(error)) from error
    require(document["entry_count"] == len(document["entries"]), "license entry count differs")
    require(document == expected, "license file inventory differs from the image")
    return canonical_sha256(document)


def write_once(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.exists() and not path.is_symlink(), "license inventory output already exists")
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def logical_output(image_root, output):
    image_root = image_root.resolve()
    output = output.resolve()
    try:
        relative = output.relative_to(image_root)
    except ValueError as error:
        raise InventoryError("output must be inside the image root") from error
    return PurePosixPath("/") / PurePosixPath(relative.as_posix())


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command")
    for name in ("create", "validate"):
        child = subparsers.add_parser(name, allow_abbrev=False)
        child.add_argument("--root", type=Path, default=Path("/"))
        child.add_argument("--release", type=Path, required=True)
        child.add_argument("--release-schema", type=Path, required=True)
        child.add_argument("--schema", type=Path, required=True)
        if name == "create":
            child.add_argument("--output", type=Path, required=True)
        else:
            child.add_argument("inventory", type=Path)
    return parser


def main(argv=None):
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.error("a command is required")
    try:
        release = load_schema_document(arguments.release, arguments.release_schema)
        if arguments.command == "create":
            output_path = logical_output(arguments.root, arguments.output)
            document = build_inventory(arguments.root, release, output_path)
            validate_inventory(document, arguments.schema, document)
            write_once(arguments.output, document)
            digest = canonical_sha256(document)
            print("wrote license file inventory: %d entries (%s)" % (len(document["entries"]), digest))
        else:
            document = VALIDATOR["load_json"](arguments.inventory)
            require(isinstance(document, dict), "license inventory must contain an object")
            inventory_path = logical_output(arguments.root, arguments.inventory)
            expected = build_inventory(arguments.root, release, inventory_path)
            digest = validate_inventory(document, arguments.schema, expected)
            print("valid license file inventory: %d entries (%s)" % (len(document["entries"]), digest))
    except (InventoryError, ValidationError, OSError, ValueError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

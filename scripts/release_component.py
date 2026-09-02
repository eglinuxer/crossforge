#!/usr/bin/env python3
"""Read one release-component projection without the full release graph.

This reader is intentionally self-contained so an early Docker stage can copy
one exact projection and this file only.  The expected component identity and
scope are mandatory trust-boundary inputs; neither is inferred from the
untrusted document.
"""

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from pathlib import Path


COMPONENT_SCHEMA_ID = (
    "https://crossforge.dev/schemas/release-component.schema.json"
)
COMPONENT_KIND = "crossforge-release-component"
COMPONENT_KEYS = {
    "$schema",
    "schema_version",
    "kind",
    "component",
    "scope",
    "dependencies",
    "materials",
}
COMPONENT_SCOPES = {"build", "qualification", "supply", "future"}
DEPENDENCY_KEYS = {"component", "canonical_sha256"}
MATERIAL_KEYS = {"path", "value"}
SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
JSON_TYPES = (
    "object",
    "array",
    "string",
    "integer",
    "number",
    "boolean",
    "null",
)


class ComponentError(ValueError):
    """A component projection or material request is invalid."""


def require(condition, message):
    if not condition:
        raise ComponentError(message)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ComponentError("duplicate JSON key: %r" % key)
        result[key] = value
    return result


def reject_nonfinite_constant(value):
    raise ComponentError("non-finite JSON number: %s" % value)


def parse_finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ComponentError("non-finite JSON number: %s" % value)
    return parsed


def load_json(path):
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(
                stream,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonfinite_constant,
                parse_float=parse_finite_float,
            )
    except (OSError, ValueError) as error:
        raise ComponentError("%s: %s" % (path, error)) from error


def validate_component_name(value, label):
    require(type(value) is str, "%s must be a string" % label)
    parts = value.split("/")
    require(
        parts and all(SEGMENT_RE.match(part) for part in parts),
        "%s has unsafe path segments: %r" % (label, value),
    )
    return value


def decode_json_pointer(pointer):
    """Validate a non-root projection pointer and return decoded tokens."""
    require(type(pointer) is str, "material path must be a string")
    require(pointer.startswith("/"), "invalid material JSON Pointer: %r" % pointer)
    raw_tokens = pointer[1:].split("/")
    require(
        raw_tokens and all(token != "" for token in raw_tokens),
        "invalid material JSON Pointer: %r" % pointer,
    )
    decoded = []
    for token in raw_tokens:
        result = []
        index = 0
        while index < len(token):
            character = token[index]
            if character != "~":
                result.append(character)
                index += 1
                continue
            require(
                index + 1 < len(token) and token[index + 1] in ("0", "1"),
                "invalid material JSON Pointer escape: %r" % pointer,
            )
            result.append("~" if token[index + 1] == "0" else "/")
            index += 2
        decoded_token = "".join(result)
        require(
            decoded_token != "..",
            "unsafe material JSON Pointer token: %r" % pointer,
        )
        decoded.append(decoded_token)
    return tuple(decoded)


def _validate_json_value(value, path="value"):
    value_type = type(value)
    if value is None or value_type in (str, int, bool):
        return
    if value_type is float:
        require(math.isfinite(value), "%s contains a non-finite number" % path)
        return
    if value_type is list:
        for index, child in enumerate(value):
            _validate_json_value(child, "%s[%d]" % (path, index))
        return
    if value_type is dict:
        for key, child in value.items():
            require(type(key) is str, "%s has a non-string object key" % path)
            _validate_json_value(child, "%s.%s" % (path, key))
        return
    raise ComponentError("%s has a non-JSON value" % path)


def _validate_dependencies(dependencies, component):
    require(type(dependencies) is list, "component dependencies must be an array")
    names = []
    for index, dependency in enumerate(dependencies):
        label = "component dependency %d" % index
        require(type(dependency) is dict, "%s must be an object" % label)
        require(set(dependency) == DEPENDENCY_KEYS, "%s fields differ" % label)
        name = validate_component_name(dependency["component"], label + " name")
        require(name != component, "component cannot depend on itself")
        digest = dependency["canonical_sha256"]
        require(
            type(digest) is str and SHA256_RE.match(digest),
            "%s has an invalid canonical SHA256" % label,
        )
        names.append(name)
    require(names == sorted(names), "component dependencies are not sorted")
    require(
        len(names) == len(set(names)),
        "component repeats a logical dependency",
    )


def _validate_materials(materials):
    require(type(materials) is list, "component materials must be an array")
    require(materials, "component projection has no materials")
    paths = []
    logical_paths = []
    for index, material in enumerate(materials):
        label = "component material %d" % index
        require(type(material) is dict, "%s must be an object" % label)
        require(set(material) == MATERIAL_KEYS, "%s fields differ" % label)
        path = material["path"]
        logical_path = decode_json_pointer(path)
        _validate_json_value(material["value"], label + " value")
        paths.append(path)
        logical_paths.append(logical_path)
    require(paths == sorted(paths), "component materials are not sorted")
    require(
        len(logical_paths) == len(set(logical_paths)),
        "component repeats a logical material path",
    )


def validate_component(document, expected_component, expected_scope):
    """Validate a document against an explicit identity and scope."""
    expected_component = validate_component_name(
        expected_component, "expected component"
    )
    require(
        type(expected_scope) is str and expected_scope in COMPONENT_SCOPES,
        "unsupported expected component scope: %r" % expected_scope,
    )
    require(type(document) is dict, "release component must be an object")
    require(set(document) == COMPONENT_KEYS, "release component envelope fields differ")
    require(
        document["$schema"] == COMPONENT_SCHEMA_ID
        and type(document["$schema"]) is str,
        "unsupported release component schema",
    )
    require(
        type(document["schema_version"]) is int
        and document["schema_version"] == 1,
        "unsupported release component schema version",
    )
    require(
        type(document["kind"]) is str and document["kind"] == COMPONENT_KIND,
        "unsupported release component kind",
    )
    component = validate_component_name(document["component"], "component")
    require(
        component == expected_component,
        "component identity differs: expected %s, found %s"
        % (expected_component, component),
    )
    require(type(document["scope"]) is str, "component scope must be a string")
    require(
        document["scope"] == expected_scope,
        "component scope differs: expected %s, found %s"
        % (expected_scope, document["scope"]),
    )
    _validate_dependencies(document["dependencies"], component)
    _validate_materials(document["materials"])
    return document


def validate_canonical_sha256(value, label="expected canonical SHA256"):
    require(
        type(value) is str and SHA256_RE.match(value),
        "%s must be 64 lowercase hexadecimal characters" % label,
    )
    return value


def validate_authentic_component(
    document, expected_component, expected_scope, expected_canonical_sha256
):
    """Validate component semantics, then bind them to a trusted digest."""
    validate_canonical_sha256(expected_canonical_sha256)
    validate_component(document, expected_component, expected_scope)
    actual = canonical_sha256(document)
    require(
        actual == expected_canonical_sha256,
        "component canonical SHA256 differs: expected %s, found %s"
        % (expected_canonical_sha256, actual),
    )
    return document


def load_component(
    path, expected_component, expected_scope, expected_canonical_sha256
):
    document = load_json(path)
    return validate_authentic_component(
        document,
        expected_component,
        expected_scope,
        expected_canonical_sha256,
    )


def json_type_matches(value, expected_type):
    require(
        type(expected_type) is str and expected_type in JSON_TYPES,
        "unsupported expected JSON type: %r" % expected_type,
    )
    value_type = type(value)
    return {
        "object": value_type is dict,
        "array": value_type is list,
        "string": value_type is str,
        "integer": value_type is int,
        "number": value_type in (int, float),
        "boolean": value_type is bool,
        "null": value is None,
    }[expected_type]


def material_value(
    document,
    expected_component,
    expected_scope,
    expected_canonical_sha256,
    pointer,
    expected_type,
):
    """Return a typed material after checking the caller's trust boundary."""
    validate_authentic_component(
        document,
        expected_component,
        expected_scope,
        expected_canonical_sha256,
    )
    logical_pointer = decode_json_pointer(pointer)
    values = {}
    for material in document["materials"]:
        values[decode_json_pointer(material["path"])] = material["value"]
    require(
        logical_pointer in values,
        "component does not own material path %s" % pointer,
    )
    value = values[logical_pointer]
    require(
        json_type_matches(value, expected_type),
        "material %s is not JSON type %s" % (pointer, expected_type),
    )
    return copy.deepcopy(value)


def read_material(
    path,
    expected_component,
    expected_scope,
    expected_canonical_sha256,
    pointer,
    expected_type,
):
    document = load_component(
        path,
        expected_component,
        expected_scope,
        expected_canonical_sha256,
    )
    return material_value(
        document,
        expected_component,
        expected_scope,
        expected_canonical_sha256,
        pointer,
        expected_type,
    )


def canonical_json(value):
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Validate and read one Crossforge release component",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command")
    for command in ("validate", "get"):
        child = subparsers.add_parser(command, allow_abbrev=False)
        child.add_argument("file")
        child.add_argument("--expected-component", required=True)
        child.add_argument(
            "--expected-scope", choices=sorted(COMPONENT_SCOPES), required=True
        )
        child.add_argument("--expected-sha256", required=True)
        if command == "get":
            child.add_argument("--path", required=True)
            child.add_argument("--type", choices=JSON_TYPES, required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.error("a command is required")
    try:
        document = load_component(
            arguments.file,
            arguments.expected_component,
            arguments.expected_scope,
            arguments.expected_sha256,
        )
        if arguments.command == "get":
            value = material_value(
                document,
                arguments.expected_component,
                arguments.expected_scope,
                arguments.expected_sha256,
                arguments.path,
                arguments.type,
            )
            sys.stdout.write(canonical_json(value) + "\n")
    except ComponentError as error:
        sys.stderr.write("release_component.py: error: %s\n" % error)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

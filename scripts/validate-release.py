#!/usr/bin/env python3
"""Validate release.json without third-party Python packages.

The validator intentionally remains compatible with Rocky 8's platform Python
so the earliest Docker stage can run it before Crossforge builds its own Python
matrix.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


class ValidationError(ValueError):
    pass


SCHEMA_KEYWORDS = {
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "$comment",
    "title",
    "description",
    "default",
    "examples",
    "type",
    "const",
    "enum",
    "allOf",
    "oneOf",
    "anyOf",
    "properties",
    "required",
    "additionalProperties",
    "minItems",
    "maxItems",
    "uniqueItems",
    "prefixItems",
    "items",
    "minLength",
    "minimum",
    "maximum",
    "pattern",
}


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_json(path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise ValidationError(f"{path}: {error}") from error


def resolve_ref(root, reference):
    if not reference.startswith("#/"):
        raise ValidationError(f"unsupported non-local schema reference: {reference}")
    value = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ValidationError(f"unresolved schema reference: {reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise ValidationError(f"schema reference is not an object: {reference}")
    return value


def is_json_type(value, expected):
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }.get(expected, lambda: False)()


def validate_schema_subset(schema, path="$"):
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValidationError("%s: schema must be an object or boolean" % path)
    unknown = sorted(set(schema) - SCHEMA_KEYWORDS)
    if unknown:
        raise ValidationError("%s: unsupported schema keywords %r" % (path, unknown))

    for keyword in ("properties", "$defs"):
        for name, child in schema.get(keyword, {}).items():
            validate_schema_subset(child, "%s.%s.%s" % (path, keyword, name))
    for keyword in ("allOf", "oneOf", "anyOf", "prefixItems"):
        for index, child in enumerate(schema.get(keyword, [])):
            validate_schema_subset(child, "%s.%s[%d]" % (path, keyword, index))
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, (dict, bool)):
            validate_schema_subset(child, "%s.%s" % (path, keyword))


def validate(value, schema, root, path):
    if schema is False:
        raise ValidationError(f"{path}: value is not allowed")
    if schema is True:
        return

    if "$ref" in schema:
        validate(value, resolve_ref(root, schema["$ref"]), root, path)

    for subschema in schema.get("allOf", []):
        validate(value, subschema, root, path)

    for keyword, expected_matches in (("oneOf", 1), ("anyOf", None)):
        if keyword not in schema:
            continue
        matches = 0
        for candidate in schema[keyword]:
            try:
                validate(value, candidate, root, path)
            except ValidationError:
                pass
            else:
                matches += 1
        if matches == 0 or (expected_matches is not None and matches != expected_matches):
            raise ValidationError(f"{path}: does not satisfy {keyword}")

    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{path}: expected {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}: {value!r} is not one of {schema['enum']!r}")

    expected_types = schema.get("type")
    if expected_types is not None:
        expected_types = [expected_types] if isinstance(expected_types, str) else expected_types
        if not any(is_json_type(value, item) for item in expected_types):
            raise ValidationError(f"{path}: expected type {expected_types!r}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValidationError(f"{path}: missing required keys {missing!r}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValidationError(f"{path}: unknown keys {unknown!r}")
        for key, child in value.items():
            if key in properties:
                validate(child, properties[key], root, f"{path}.{key}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValidationError(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(f"{path}: too many items")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(canonical) != len(set(canonical)):
                raise ValidationError(f"{path}: duplicate array item")
        prefix_items = schema.get("prefixItems", [])
        for index, child_schema in enumerate(prefix_items):
            if index < len(value):
                validate(value[index], child_schema, root, f"{path}[{index}]")
        if "items" in schema:
            start = len(prefix_items)
            for index, child in enumerate(value[start:], start=start):
                validate(child, schema["items"], root, f"{path}[{index}]")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValidationError(f"{path}: string is too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValidationError(f"{path}: does not match {schema['pattern']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path}: must be at least {schema['minimum']!r}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path}: must be at most {schema['maximum']!r}")


def find_pending(value, path="$"):
    pending = []
    if isinstance(value, dict):
        if value.get("status") == "pending":
            pending.append(path)
        for key, child in value.items():
            pending.extend(find_pending(child, "%s.%s" % (path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            pending.extend(find_pending(child, "%s[%d]" % (path, index)))
    return pending


def main():
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path, default=repository / "config/release.json")
    parser.add_argument(
        "--schema",
        type=Path,
        default=repository / "config/schemas/release.schema.json",
    )
    parser.add_argument(
        "--require-locked",
        action="store_true",
        help="reject skeleton configurations that still contain pending source pins",
    )
    arguments = parser.parse_args()

    try:
        config = load_json(arguments.config)
        schema = load_json(arguments.schema)
        if not isinstance(config, dict) or not isinstance(schema, dict):
            raise ValidationError("configuration and schema roots must be JSON objects")
        validate_schema_subset(schema)
        validate(config, schema, schema, "$")
        pending = find_pending(config)
        if arguments.require_locked and pending:
            raise ValidationError("pending source pins: %s" % ", ".join(pending))
    except ValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    state = "locked" if not pending else "%d pending pin(s)" % len(pending)
    print(f"valid: {arguments.config} (canonical sha256:{digest}; {state})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

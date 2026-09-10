"""Explicit component build/test input identities, independent of source commit.

An input document is a declared material set, not automatic dependency discovery.
The producer/planner owns selecting the complete set. Consumers compare against
an independently computed document, never a subset supplied by an artifact.
"""

import copy
import re

from .identity import (content_sha256, digest_value, exact_fields, file_record,
                       relative_path, require)


SCHEMA = "https://crossforge.dev/schemas/component-inputs.schema.json"
TARGETS = {"x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"}
SCOPES = {"build", "qualification", "supply"}
COMPONENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)+\Z")


def validate(document):
    exact_fields(document, ("$schema", "schema_version", "kind", "component", "scope",
                            "platform", "targets", "parameters", "files", "dependencies"),
                 "component inputs")
    require(document["$schema"] == SCHEMA, "component input schema differs")
    require(type(document["schema_version"]) is int and document["schema_version"] == 1,
            "unsupported component input schema version")
    require(document["kind"] == "crossforge-component-inputs", "component input kind differs")
    require(type(document["component"]) is str and COMPONENT_NAME.fullmatch(document["component"]),
            "component name is invalid")
    require(type(document["scope"]) is str and document["scope"] in SCOPES,
            "component scope is invalid")
    require(document["platform"] == "linux/amd64", "unsupported component host platform")
    targets = document["targets"]
    require(type(targets) is list and all(type(t) is str and t in TARGETS for t in targets),
            "unsupported component targets")
    require(targets == sorted(set(targets)), "component targets must be unique and sorted")
    require(type(document["parameters"]) is dict, "component parameters must be an object")
    content_sha256(document["parameters"])
    files = document["files"]
    require(type(files) is list and files, "component input files must be a nonempty array")
    for record in files:
        exact_fields(record, ("path", "sha256", "mode"), "input file")
        relative_path(record["path"])
        digest_value(record["sha256"], "input file SHA256")
        require(type(record["mode"]) is str and re.fullmatch(r"[0-7]{4}", record["mode"]),
                "input file mode is invalid")
    paths = [record["path"] for record in files]
    require(paths == sorted(set(paths)), "input files must be unique and sorted")
    dependencies = document["dependencies"]
    require(type(dependencies) is list, "component dependencies must be an array")
    for record in dependencies:
        exact_fields(record, ("component", "inputs_sha256", "artifact_digest"), "dependency")
        name = record["component"]
        require(type(name) is str and COMPONENT_NAME.fullmatch(name), "dependency name is invalid")
        require(name != document["component"], "component cannot depend on itself")
        digest_value(record["inputs_sha256"], "dependency inputs SHA256")
        digest_value(record["artifact_digest"], "dependency artifact digest", oci=True)
    names = [record["component"] for record in dependencies]
    require(names == sorted(set(names)), "dependencies must be unique and sorted")
    return document


def capture(root, component, scope, paths, targets=(), parameters=None, dependencies=()):
    require(type(paths) in (list, tuple) and paths, "declare input file paths explicitly")
    for path in paths:
        relative_path(path)
    require(len(paths) == len(set(paths)), "duplicate declared input path")
    require(type(targets) in (list, tuple) and all(type(t) is str for t in targets),
            "declare component targets as an array of strings")
    require(type(dependencies) in (list, tuple), "declare dependencies as an array")
    for record in dependencies:
        exact_fields(record, ("component", "inputs_sha256", "artifact_digest"), "dependency")
        require(type(record["component"]) is str, "dependency name must be a string")
    result = {"$schema": SCHEMA, "schema_version": 1,
              "kind": "crossforge-component-inputs", "component": component, "scope": scope,
              "platform": "linux/amd64", "targets": sorted(targets),
              "parameters": copy.deepcopy(parameters if parameters is not None else {}),
              "files": [file_record(root, path) for path in sorted(paths)],
              "dependencies": sorted(copy.deepcopy(list(dependencies)),
                                     key=lambda record: record["component"])}
    return validate(result)


def identity(document):
    return content_sha256(validate(document))


def require_match(recorded, expected):
    """Expected must come from the current planner's full material closure."""
    require(identity(recorded) == identity(expected), "component inputs differ")


def verify_files(document, root):
    """Check declared bytes/modes; this does not assert closure completeness."""
    validate(document)
    for record in document["files"]:
        require(file_record(root, record["path"]) == record,
                "component input file differs: %s" % record["path"])
